"""Rust source without a parser: // and nested /* */ comments (and, when asked, string contents) blanked with offsets
kept; raw strings (r"…", r#"…"#), byte strings and char literals understood, lifetimes ('a) left alone; string
literals read back out; functions, impl blocks and statements found by brace matching. Standard library only."""
from __future__ import annotations

import re
from functools import lru_cache

from langs.common import match_close

RAW = re.compile(r"b?r(#*)\"")
CHAR = re.compile(r"b?'(?:\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]{1,6}\}|.)|[^'\\\n])'")
LITERAL = re.compile(r"b?r(#*)\"(.*?)\"\1|b?\"((?:[^\"\\]|\\.)*)\"", re.S)
FN = re.compile(r"\bfn\s+(\w+)")
IMPL = re.compile(r"\bimpl\b\s*(?:<[^{]*?>)?\s*(?:[\w:<>,\s&'()]+?\s+for\s+)?([\w:]+)\s*(?:<[^{]*?>)?\s*(?:where\b[^{]*)?\{")


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
        if c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            _blank(out, i, j)
            i = j
        elif c == "/" and text.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            _blank(out, i, j)
            i = j
        elif c in "rb" and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")) and (m := RAW.match(text, i)):
            close = text.find('"' + m.group(1), m.end())
            close = n if close < 0 else close
            if strings:
                _blank(out, m.end(), close)
            i = close + 1 + len(m.group(1))
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if strings:
                _blank(out, i + 1, j)
            i = j + 1
        elif c == "'" and (m := CHAR.match(text, i)):
            if strings:
                _blank(out, i + 1, m.end() - 1)
            i = m.end()
        else:
            i += 1
    return "".join(out)


def literals(args: str) -> list[str]:
    """String literals in source order (plain, raw and byte strings), unescaped only for raw ones."""
    return [m.group(2) if m.group(2) is not None else m.group(3) for m in LITERAL.finditer(args)]


def chained(bare: str) -> str:
    """bare with `::` read as a '.' receiver step (offsets kept): sqlx::query( is a call query on [sqlx]."""
    return bare.replace("::", " .")


def fn_spans(bare: str) -> list[tuple[str, int, int, int]]:
    """(name, where the name is, body start, body end) of every fn with a body."""
    out = []
    for m in FN.finditer(bare):
        open_ = bare.find("(", m.end())
        if open_ < 0:
            continue
        close = match_close(bare, open_, "(", ")")
        k = close + 1
        depth = 0
        while k < len(bare):
            ch = bare[k]
            if ch == "<":
                depth += 1
            elif ch == ">" and bare[k - 1] != "-":
                depth -= 1
            elif ch in "{;" and depth <= 0:
                break
            k += 1
        if k < len(bare) and bare[k] == "{":
            out.append((m.group(1), m.start(1), k, match_close(bare, k) + 1))
    return out


def impl_spans(bare: str) -> list[tuple[str, int, int]]:
    """(type name, body start, body end) of every impl block."""
    return [(m.group(1).split("::")[-1], m.end() - 1, match_close(bare, m.end() - 1)) for m in IMPL.finditer(bare)]


def enclosing_paren(bare: str, pos: int) -> int:
    """The end of the innermost (…) that contains pos, else the end of its statement (';')."""
    depth = 0
    for k in range(pos - 1, -1, -1):
        ch = bare[k]
        if ch in ")]}":
            depth += 1
        elif ch in "([{":
            if depth == 0:
                if ch == "(":
                    return match_close(bare, k, "(", ")")
                break
            depth -= 1
    semi = bare.find(";", pos)
    return len(bare) if semi < 0 else semi


def statement_spans(bare: str) -> list[tuple[str, int, int]]:
    """(variable, start, end) of `let x = …;` statements."""
    out = []
    for m in re.finditer(r"\blet\s+(?:mut\s+)?(\w+)\s*(?::[^=;]+)?=", bare):
        depth, k = 0, m.end()
        while k < len(bare):
            ch = bare[k]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == ";" and depth <= 0:
                break
            k += 1
        out.append((m.group(1), m.start(), k))
    return out


def snake_words(cond: str) -> str:
    """A Rust condition with snake_case names in camelCase, so langs.flowkinds.phrase reads it (x.is_empty() -> x.isEmpty())."""
    return re.sub(r"\b([a-z][a-z0-9]*)((?:_[a-z0-9]+)+)\b", lambda m: m.group(1) + "".join(p.capitalize() for p in m.group(2).split("_") if p),
                  cond.replace("::", "."))
