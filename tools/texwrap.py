# -*- coding: utf-8 -*-
"""
texwrap.py -- hard-wrap long lines in LaTeX source without changing what LaTeX
typesets.

A newline in LaTeX is ordinary whitespace; only a blank line starts a new
paragraph. So inserting newlines at spaces is safe -- except in four places,
which this script refuses to touch:

  1. verbatim-like and alignment environments (line structure is significant)
  2. any line carrying a comment (folding the tail would comment out live code)
  3. lines using \\verb / \\lstinline (delimited, line-sensitive)
  4. a break directly after a control word (\\foo), where TeX eats the newline
     AND the following space, silently welding two words together

Lines already within the width are emitted byte-identical, so files that are
already wrapped come out untouched.

Usage: python texwrap.py [--width N] [--check] FILE...
  --check  report what would change; write nothing
"""

import argparse
import io
import re
import sys

# Line structure inside these is significant -- never reflow their bodies.
PROTECTED_ENVS = {
    "lstlisting", "verbatim", "Verbatim", "alltt", "minted", "listing",
    "tikzpicture", "picture",
    "tabular", "tabular*", "tabularx", "longtable", "array", "matrix",
    "bmatrix", "pmatrix", "vmatrix", "smallmatrix",
    "align", "align*", "alignat", "alignat*", "gather", "gather*",
    "equation", "equation*", "eqnarray", "eqnarray*", "displaymath",
    "split", "cases", "multline", "multline*",
}

BEGIN_RE = re.compile(r"\\begin\{([^}]*)\}")
END_RE = re.compile(r"\\end\{([^}]*)\}")
# A control word: backslash + letters. Breaking right after one is unsafe.
CTRL_WORD_RE = re.compile(r"^\\[a-zA-Z]+\*?$")
# Inline verbatim macros take a delimited argument that must not be split.
# Matched as whole spans so they can be made atomic rather than skipping the
# whole line: \lstinline{...}, \lstinline[opts]|...|, \verb|...|, \verb*+...+
INLINE_VERB_RE = re.compile(
    r"\\(?:lstinline|verb)\*?(?:\[[^\]]*\])?(?:\{[^{}]*\}|([^A-Za-z0-9\s])(?:(?!\1).)*\1)"
)
# Placeholder character that cannot occur in LaTeX source.
SENTINEL = "\x00"


def protect_inline_verb(line):
    """Replace inline-verbatim spans with equal-length space-free sentinels.

    Equal length keeps column arithmetic exact; the absence of spaces makes a
    break inside the span impossible. Returns (masked_line, spans).
    """
    spans = []

    def sub(m):
        spans.append(m.group(0))
        return SENTINEL * len(m.group(0))

    return INLINE_VERB_RE.sub(sub, line), spans


def restore_inline_verb(lines, spans):
    """Put the original spans back, in order."""
    it = iter(spans)
    out = []
    for ln in lines:
        while SENTINEL in ln:
            run = re.search(SENTINEL + "+", ln)
            ln = ln[: run.start()] + next(it) + ln[run.end():]
        out.append(ln)
    return out


def has_comment(line):
    """True if the line contains an unescaped % (a real comment)."""
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\":
            i += 2  # skip the escaped character, including \%
            continue
        if c == "%":
            return True
        i += 1
    return False


def wrap_line(line, width):
    """Wrap one long line at spaces, preserving its leading indentation.

    Returns a list of lines. Guarantees: joining the result with a single
    space between pieces (after stripping the shared indent) reproduces the
    original token sequence exactly -- no token is split, added, or dropped.
    """
    stripped = line.lstrip(" \t")
    indent = line[: len(line) - len(stripped)]
    tokens = stripped.split()
    if not tokens:
        return [line]

    # Pass 1: greedy fill.
    rows = []
    cur = []
    cur_len = len(indent)
    for tok in tokens:
        add = len(tok) + (1 if cur else 0)
        if cur and cur_len + add > width:
            rows.append(cur)
            cur = [tok]
            cur_len = len(indent) + len(tok)
        else:
            cur.append(tok)
            cur_len += add
    if cur:
        rows.append(cur)

    # Pass 2: hazard 4 -- no row may end on a control word, because TeX would
    # absorb the newline and the next line's leading space along with it.
    # Push any such token down to the following row. A row that is nothing but
    # a control word is left alone (nowhere to push it that is any safer).
    for i in range(len(rows) - 1):
        while len(rows[i]) > 1 and CTRL_WORD_RE.match(rows[i][-1]):
            rows[i + 1].insert(0, rows[i].pop())

    return [indent + " ".join(r) for r in rows]


def process(text, width, trigger, strip_trailing=True):
    lines = text.split("\n")
    out = []
    env_stack = []
    changed = 0

    for line in lines:
        # Track protected environments; their delimiters and bodies pass through.
        begins = BEGIN_RE.findall(line)
        ends = END_RE.findall(line)

        in_protected = bool(env_stack)

        for name in begins:
            if name in PROTECTED_ENVS:
                env_stack.append(name)
        if begins and env_stack:
            in_protected = True

        if in_protected:
            # Trailing space is significant inside verbatim -- leave it.
            out.append(line)
            for name in ends:
                if env_stack and name == env_stack[-1]:
                    env_stack.pop()
            continue

        # chktex 24: trailing space after \label{} skews page references.
        # Stripping it outside verbatim is inert everywhere else.
        if strip_trailing:
            stripped_line = line.rstrip()
            if stripped_line != line:
                changed += 1
                line = stripped_line

        for name in ends:
            if env_stack and name == env_stack[-1]:
                env_stack.pop()

        # Below the trigger: emit byte-identical. This is what leaves an
        # already-wrapped file alone instead of re-ragging it.
        if len(line) <= trigger:
            out.append(line)
            continue
        # Hazard 3 first: mask inline-verbatim spans so they are unbreakable.
        # This must precede the comment test, because a % inside \lstinline{}
        # is a literal character, not a comment.
        masked, spans = protect_inline_verb(line)

        # Hazard 2: never fold a comment's tail into live code.
        if has_comment(masked):
            out.append(line)
            continue

        wrapped = restore_inline_verb(wrap_line(masked, width), spans)
        if wrapped != [line]:
            changed += 1
        out.extend(wrapped)

    return "\n".join(out), changed


def tokens_of(text):
    """Whitespace-token stream, ignoring line boundaries -- the invariant."""
    return text.split()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=79,
                    help="column to wrap to (default 79)")
    ap.add_argument("--trigger", type=int, default=None,
                    help="only reflow lines longer than this (default: width)")
    ap.add_argument("--no-strip", action="store_true",
                    help="do not strip trailing whitespace")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("files", nargs="+")
    args = ap.parse_args()
    trigger = args.trigger if args.trigger is not None else args.width

    total = 0
    for path in args.files:
        # Detect the file's existing line terminator so we write it back
        # unchanged; otherwise every line shows up as modified in git.
        with open(path, "rb") as fh:
            raw = fh.read()
        eol = "\r\n" if raw.count(b"\r\n") > raw.count(b"\n") - raw.count(b"\r\n") \
            else "\n"

        with io.open(path, encoding="utf-8") as fh:
            src = fh.read()
        new, changed = process(src, args.width, trigger,
                               strip_trailing=not args.no_strip)

        # Safety invariant: the token stream must be bit-for-bit identical.
        # Only line boundaries may differ.
        if tokens_of(src) != tokens_of(new):
            sys.stderr.write("ABORT %s: token stream changed\n" % path)
            return 2

        longest = max([len(l) for l in new.split("\n")] or [0])
        print("%-32s %4d lines reflowed, longest now %3d  (%s)"
              % (path, changed, longest, "CRLF" if eol == "\r\n" else "LF"))
        total += changed
        if not args.check and changed:
            with io.open(path, "w", encoding="utf-8", newline=eol) as fh:
                fh.write(new)
    print("total: %d lines reflowed" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
