"""Ruby source without a parser: comments and string contents blanked (offsets kept), `def … end` blocks found by
keyword matching (modifier if/unless/while ignored), statements joined across continuation lines, and conditions in
plain words. Standard library only."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from langs.common import line_of

HEREDOC = re.compile(r"<<([~-]?)(['\"]?)([A-Za-z_]\w*)\2")
PERCENT = re.compile(r"%[qQwWiIrs]?([(\[{<|!/])")
PAIRS = {"(": ")", "[": "]", "{": "}", "<": ">"}
REGEX_AFTER = re.compile(r"(?:^|[(,=~!|&{\[;]|\b(?:when|if|unless|and|or|return|split|match|scan|sub|gsub))\s*$")
KEYWORD = re.compile(r"(?<![.\w$@:])(def|class|module|if|unless|while|until|case|begin|do|for|end)(?![\w?!:])")
OPENS_EXPR = "=([,|&{"


def line(text: str, pos: int) -> int:
    return line_of(text, pos)


def _blank(out: list[str], a: int, b: int) -> None:
    for k in range(a, min(b, len(out))):
        if out[k] != "\n":
            out[k] = " "


def _string_end(text: str, i: int, quote: str, close: str | None = None) -> int:
    """Index just past the string starting at text[i] (its opening delimiter); nested pairs and #{…} respected."""
    close = close or quote
    depth, j, n = 0, i + 1, len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if quote == '"' and text.startswith("#{", j):
            k = _interp_end(text, j + 2)
            j = k
            continue
        if c == quote and close != quote:
            depth += 1
        elif c == close:
            if depth == 0:
                return j + 1
            depth -= 1
        j += 1
    return n


def _interp_end(text: str, j: int) -> int:
    depth, n = 1, len(text)
    while j < n:
        c = text[j]
        if c in "\"'":
            j = _string_end(text, j, c)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


def _heredoc_bodies(text: str, out: list[str], strings: bool, pending: list[tuple[str, str]], i: int) -> int:
    """Blank the bodies of the heredocs opened on the line ending at i; return where the code resumes."""
    j = i + 1
    for squiggly, ident in pending:
        while j < len(text):
            end = text.find("\n", j)
            end = len(text) if end < 0 else end
            body_line = text[j:end]
            done = (body_line.strip() if squiggly else body_line) == ident
            if strings and not done:
                _blank(out, j, end)
            j = end + 1
            if done:
                break
    return j


@lru_cache(maxsize=32)
def blank(text: str, strings: bool = False) -> str:
    """text with # comments, =begin/=end blocks and __END__ data blanked, and (when asked) the inside of string, symbol,
    %-literal, regex and heredoc literals too. Line numbers and offsets stay the same."""
    out = list(text)
    i, n = 0, len(text)
    pending: list[tuple[str, str]] = []
    while i < n:
        c = text[i]
        at_line = i == 0 or text[i - 1] == "\n"
        if c == "\n":
            if pending:
                i = _heredoc_bodies(text, out, strings, pending, i)
                pending = []
                continue
            i += 1
        elif at_line and text.startswith("=begin", i):
            j = text.find("\n=end", i)
            j = n if j < 0 else j + 5
            _blank(out, i, j)
            i = j
        elif at_line and text.startswith("__END__", i):
            _blank(out, i, n)
            i = n
        elif c == "#":
            j = text.find("\n", i)
            j = n if j < 0 else j
            _blank(out, i, j)
            i = j
        elif c in "\"'`":
            j = _string_end(text, i, c)
            if strings:
                _blank(out, i + 1, j - 1)
            i = j
        elif c == "<" and (h := HEREDOC.match(text, i)) and (h.group(1) or h.group(3).isupper()) and not text[max(0, i - 1):i].isalnum():
            pending.append((h.group(1), h.group(3)))
            i = h.end()
        elif c == "%" and (p := PERCENT.match(text, i)) and (text[i + 1] in "wWiIqQrs" or not re.search(r"[\w)\]}]\s*$", text[max(0, i - 40):i])):
            # %w[…] / %i(…) are always literals (`puts %w[do end]`); a bare %( after a value is the modulo operator
            d = p.group(1)
            j = _string_end(text, p.end() - 1, d, PAIRS.get(d, d))
            if strings:
                _blank(out, p.end(), j - 1)
            i = j
        elif c == "/" and REGEX_AFTER.search(text[max(0, i - 40):i]) and not text.startswith("/ ", i):
            j = i + 1
            while j < n and text[j] not in "/\n":
                j += 2 if text[j] == "\\" else 1
            if strings:
                _blank(out, i + 1, j)
            i = j + 1
        elif c == "?" and i + 1 < n and text[i + 1] in "\"'`" and i > 0 and text[i - 1] in " (,":
            i += 2  # a character literal: ?" or ?'
        else:
            i += 1
    return "".join(out)


@dataclass
class Block:
    kw: str
    pos: int    # where the opening keyword is
    end: int    # where its `end` keyword is (len(text) when unclosed)
    head: int   # end of the opening line (where the body starts)


def _statement_head(bare: str, pos: int) -> str:
    start = max(bare.rfind("\n", 0, pos), bare.rfind(";", 0, pos)) + 1
    return bare[start:pos].strip()


def is_modifier(bare: str, pos: int) -> bool:
    """An if/unless/while/until/rescue that follows a statement on the same line (`x = 1 if y`)."""
    head = _statement_head(bare, pos)
    return bool(head) and head[-1] not in OPENS_EXPR and not re.search(r"\b(?:then|do|else|begin|and|or|not)$", head)


ENDLESS_DEF = re.compile(r"def\s+(?:self\.)?[\w?!=\[\]<>+\-*/%~]+?\s*(?:\([^()\n]*\))?\s*=(?![=~>])")


@lru_cache(maxsize=32)
def blocks(bare: str) -> list[Block]:
    """Every keyword block in source order of its opener. bare is blank(text, True)."""
    stack: list[Block] = []
    out: list[Block] = []
    skip_do_until = -1
    for m in KEYWORD.finditer(bare):
        kw, pos = m.group(1), m.start()
        if kw == "end":
            if stack:
                b = stack.pop()
                b.end = pos
                out.append(b)
            continue
        if kw == "do" and pos < skip_do_until:
            continue
        if kw in ("if", "unless", "while", "until") and is_modifier(bare, pos):
            continue
        if kw == "def" and ENDLESS_DEF.match(bare, pos):
            continue
        eol = bare.find("\n", pos)
        eol = len(bare) if eol < 0 else eol
        if kw in ("while", "until", "for"):
            skip_do_until = eol
        stack.append(Block(kw, pos, len(bare), eol))
    out += stack
    return sorted(out, key=lambda b: b.pos)


def statements(code: str) -> list[tuple[int, int]]:
    """(start, end) of each statement: a line, joined with the next ones while it ends with , \\ ( [ { => or a bracket
    is still open. code has comments blanked; strings may be kept."""
    out: list[tuple[int, int]] = []
    i, n = 0, len(code)
    bare = blank(code, True)
    while i < n:
        start, depth = i, 0
        while True:
            end = code.find("\n", i)
            end = n if end < 0 else end
            seg = bare[i:end]
            depth += sum(seg.count(o) for o in "([{") - sum(seg.count(c) for c in ")]}")
            tail = seg.rstrip()
            i = end + 1
            if i >= n or not (depth > 0 or tail.endswith((",", "\\", "(", "[", "{", "=>", "||", "&&"))):
                break
        if code[start:end].strip():
            out.append((start, end))
    return out


def snake(name: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)).lower()


def camel(name: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in name.split("_") if p)


def singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if re.search(r"(s|x|ch|sh)es$", word):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def plural(word: str) -> str:
    if re.search(r"[^aeiou]y$", word):
        return word[:-1] + "ies"
    if re.search(r"(s|x|ch|sh)$", word):
        return word + "es"
    return word + "s"


def _words(expr: str) -> str:
    e = re.sub(r"params\[\s*:(\w+)\s*\]", r"\1", expr.strip())
    e = re.sub(r"\(\s*\)", "", e)
    parts = [p.lstrip("@$") for p in e.split(".") if p]
    if not parts or not all(re.fullmatch(r"\w+[?!]?", p) for p in parts):
        return ""
    return " ".join(p.rstrip("?!").replace("_", " ") for p in parts)


def phrase(cond: str, negate: bool = False) -> str:
    """A Ruby condition in plain words (`params[:limit].present?` -> 'limit is set'), or '' when it isn't simple."""
    c = re.sub(r"\s+", " ", re.sub(r"\bthen\b.*$", "", cond.strip())).strip()
    joiner = re.search(r"&&|\|\||\band\b|\bor\b", c)
    if joiner and not negate:
        parts = [phrase(p) for p in re.split(r"&&|\|\||\band\b|\bor\b", c)]
        return (" and " if joiner.group(0) in ("&&", "and") else " or ").join(parts) if all(parts) else ""
    if c.startswith(("!", "not ")):
        return phrase(re.sub(r"^(!|not )\s*", "", c), not negate)
    out = _phrase(c)
    if not negate or not out:
        return out
    for a, b in ((" is set", " is missing"), (" is missing", " is set"), (" is empty", " is not empty")):
        if out.endswith(a):
            return out[: -len(a)] + b
    return out.replace(" is ", " is not ", 1) if " is " in out else f"not {out}"


def _phrase(c: str) -> str:
    m = re.fullmatch(r"(.+?)\.(nil|blank|empty|present|any|none|zero)\?", c)
    if m:
        w = _words(m.group(1))
        verb = {"nil": "is missing", "blank": "is missing", "empty": "is empty", "present": "is set", "any": "is set",
                "none": "is empty", "zero": "is zero"}[m.group(2)]
        return f"{w} {verb}" if w else ""
    m = re.fullmatch(r"(.+?)\s*(==|!=|>=|<=|>|<)\s*(.+)", c)
    if m:
        left, right = _words(m.group(1)), m.group(3).strip()
        r = right if re.fullmatch(r"-?[\d.]+|'[^']*'|\"[^\"]*\"|true|false|:\w+", right) else _words(right)
        verb = {">": "is above", "<": "is below", ">=": "is at least", "<=": "is at most", "==": "is", "!=": "is not"}[m.group(2)]
        return f"{left} {verb} {r.lstrip(':')}" if left and r else ""
    m = re.fullmatch(r"(.+?)\.(\w+)\?", c)
    if m and _words(m.group(1)):
        return f"{_words(m.group(1))} is {m.group(2).replace('_', ' ')}"
    w = _words(c)
    if not w:
        return ""
    if c.endswith("?"):
        return f"it is {w}"
    return f"{w} succeeds" if "." in c else f"{w} is set"


def rescue_text(spec: str) -> str:
    """rescue Foo::BarError => e -> 'a bar error happens'; bare rescue or StandardError -> 'it fails'."""
    names = [n.strip().split("::")[-1] for n in re.sub(r"=>.*$", "", spec).split(",") if n.strip()]
    names = [n for n in names if re.fullmatch(r"[A-Z]\w*", n)]
    if not names or any(n in ("StandardError", "Exception", "RuntimeError", "Error") for n in names):
        return "it fails"
    spaced = " or ".join(re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", re.sub(r"(Exception|Error)$", "", n)).lower() for n in names)
    return f"a {spaced} error happens"
