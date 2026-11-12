"""PHP source without a parser: // # and /* */ comments (and, when asked, string and heredoc contents) blanked with
offsets kept, string literals in either quote, and the brace/paren matching the route and flow readers share."""
from __future__ import annotations

import re
from functools import lru_cache

HEREDOC = re.compile(r"<<<[ \t]*(['\"]?)([A-Za-z_]\w*)\1[ \t]*\n")
LITERAL = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"", re.S)


def _blank(out: list[str], a: int, b: int) -> None:
    for k in range(a, min(b, len(out))):
        if out[k] != "\n":
            out[k] = " "


@lru_cache(maxsize=64)
def blank(text: str, strings: bool = False) -> str:
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if (c == "/" and text.startswith("//", i)) or (c == "#" and not text.startswith("#[", i)):
            j = text.find("\n", i)
            j = n if j < 0 else j
            _blank(out, i, j)
            i = j
        elif c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            _blank(out, i, j)
            i = j
        elif c == "<" and (h := HEREDOC.match(text, i)):
            end = re.compile(r"^[ \t]*" + re.escape(h.group(2)) + r"\b", re.M).search(text, h.end())
            j = end.start() if end else n
            if strings:
                _blank(out, h.end(), j)
            i = end.end() if end else n
        elif c in "'\"":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            if strings:
                _blank(out, i + 1, j)
            i = j + 1
        else:
            i += 1
    return "".join(out)


def literals(args: str) -> list[str]:
    """The string literals in an argument list, single- or double-quoted, in order."""
    return [m.group(1) if m.group(1) is not None else m.group(2) for m in LITERAL.finditer(args)]


def first_literal(args: str) -> str | None:
    found = literals(args)
    return found[0] if found else None


@lru_cache(maxsize=64)
def chained(bare: str) -> str:
    """bare with -> ?-> and :: turned into '.' (padded with ! and ?, which langs.flowparse.receiver_chain skips) and
    backslashes into spaces, keeping every offset: $this->a->b( reads as a call b on [a]."""
    return bare.replace("?->", "?!.").replace("->", "!.").replace("::", "!.").replace("\\", " ")


@lru_cache(maxsize=64)
def wordy(code: str) -> str:
    """code with $ dropped and -> :: \\ turned into ?. / . so conditions read as words (langs.flowkinds.phrase)."""
    return code.replace("?->", " ?.").replace("->", "?.").replace("::", "?.").replace("$", " ").replace("\\", ".")


def statement_end(bare: str, i: int) -> int:
    """Where the statement that starts at i ends: its ';' at bracket depth 0 (closures inside are skipped)."""
    depth = 0
    for k in range(i, len(bare)):
        ch = bare[k]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return k
        elif ch == ";" and depth == 0:
            return k
    return len(bare)
