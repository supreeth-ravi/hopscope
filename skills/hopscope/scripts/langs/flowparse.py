"""Classes, fields, methods and their bodies in a brace language file (Java, Kotlin, C#), read with regular expressions
and brace matching only. Used by braceflow.py to follow a route handler's calls. Standard library only."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from langs import flowkinds as fk
from langs.common import blanked, class_span, line_of, match_close, split_top

KEYWORDS = {"if", "for", "while", "switch", "catch", "using", "lock", "foreach", "synchronized", "return", "new", "when", "else", "try",
            "nameof", "typeof", "sizeof", "base", "this", "super", "throw", "await", "default", "fixed", "checked", "unchecked", "do"}
CLASS_DECL = re.compile(r"\b(class|interface|record|struct|object|enum)\s+(\w+)")
METHOD_HEAD = re.compile(r"\b(\w+)\s*(?:<[^<>(){};]*(?:<[^<>(){};]*>[^<>(){};]*)*>)?\s*\(")
JAVA_FIELD = re.compile(r"(?:^|[;{}\n])\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:(?:private|protected|public|internal|final|static|readonly|volatile|transient|required)\s+)*"
                        r"([A-Z][\w.]*(?:<[^;=(){}]*>)?\??(?:\[\])?)\s+(\w+)\s*(?:=[^;]*)?;")
KOTLIN_PROP = re.compile(r"\b(?:val|var)\s+(\w+)\s*:\s*([A-Z][\w.]*(?:<[^=\n]*?>)?\??)")
KOTLIN_NEW = re.compile(r"\b(?:val|var)\s+(\w+)\s*=\s*([A-Z]\w*)\s*\(")
LOCAL = re.compile(r"(?:^|[;{}(\n,])\s*(?:final\s+)?([A-Z][\w.]*(?:<[^;=(){}]*>)?\??)\s+(\w+)\s*(?:=|;|:(?!:))")
VAR_NEW = re.compile(r"\b(?:var|val|auto)\s+(\w+)\s*=\s*(?:new\s+)?([A-Z]\w*)\s*(?:<[^<>;]*>)?\s*\(")
ANNOT = re.compile(r"@\w+(?:\.\w+)*(?:\([^)]*\))?|\[[^\]]*\]")


@dataclass
class Method:
    name: str
    file: str
    line: int
    body: tuple[int, int]               # [start, end) in the file text
    params: dict[str, str] = field(default_factory=dict)
    owner: str = ""


@dataclass
class Klass:
    name: str
    file: str
    kind: str
    bases: list[str] = field(default_factory=list)
    base_args: list[str] = field(default_factory=list)  # generic arguments of the first base (JpaRepository<Roast, Long>)
    fields: dict[str, str] = field(default_factory=dict)
    methods: dict[str, list[Method]] = field(default_factory=dict)
    annotations: str = ""
    span: tuple[int, int] = (0, 0)


@dataclass
class Parsed:
    text: str
    code: str      # comments blanked
    bare: str      # comments and string contents blanked
    classes: list[Klass] = field(default_factory=list)
    methods: list[Method] = field(default_factory=list)


def simple_type(t: str) -> str:
    """Foo<Bar>? -> Foo; a.b.Foo -> Foo; Foo[] -> Foo."""
    t = re.sub(r"<.*", "", t.strip()).rstrip("?[]! ").split(".")[-1]
    return t


def generic_args(t: str) -> list[str]:
    m = re.search(r"<(.*)>", t)
    return [simple_type(a) for a in split_top(m.group(1))] if m else []


def params_of(text: str) -> dict[str, str]:
    """Parameter name -> type, for Java/C# (`Type name`) and Kotlin (`name: Type`) parameter lists."""
    out = {}
    for part in split_top(ANNOT.sub(" ", text)):
        part = re.sub(r"=.*$", "", part, flags=re.S).strip()
        part = re.sub(r"^(?:(?:final|ref|out|in|params|this|private|public|protected|internal|readonly|val|var|override|vararg)\s+)+", "", part)
        m = re.match(r"(\w+)\s*:\s*([\w.<>?,\s\[\]]+)$", part)
        if m:
            out[m.group(1)] = m.group(2).strip()
            continue
        m = re.match(r"([\w.<>?,\s\[\]]+?)\s+(\w+)$", part)
        if m:
            out[m.group(2)] = m.group(1).strip()
    return out


def _shallow(bare: str, start: int, end: int) -> str:
    """bare[start:end] with the inside of every nested {...} blanked, so only member declarations remain."""
    out = list(bare[start:end])
    i = 0
    while i < len(out):
        if out[i] == "{":
            close = match_close(bare, start + i) - start
            for k in range(i + 1, min(close, len(out))):
                if out[k] != "\n":
                    out[k] = " "
            i = close
        i += 1
    return "".join(out)


def _bases(header: str) -> tuple[list[str], list[str]]:
    header = re.sub(r"^\s*(?:<[^{]*?>)?\s*(?:\([^{]*?\))?", "", header, count=1)
    m = re.search(r"(?:\bextends\b|\bimplements\b|:)\s*(.*)", header, re.S)
    if not m:
        return [], []
    raw = re.sub(r"\bimplements\b|\bextends\b|\bwhere\b.*", ",", m.group(1), flags=re.S)
    parts = [p.strip() for p in split_top(raw) if p.strip()]
    return [simple_type(re.sub(r"\(.*", "", p, flags=re.S)) for p in parts], (generic_args(parts[0]) if parts else [])


def _methods(p: Parsed, k: Klass, start: int, end: int) -> None:
    shallow = _shallow(p.bare, start, end)
    for m in METHOD_HEAD.finditer(shallow):
        name = m.group(1)
        if name in KEYWORDS:
            continue
        open_ = start + m.end() - 1
        close = match_close(p.bare, open_, "(", ")")
        rest = p.bare[close + 1:close + 200]
        tail = re.match(r"\s*(?:throws\s+[\w.,\s]+?|:\s*[\w.<>?,\s\[\]]+?|where\s+[^{]+?)?\s*(\{|=>|=(?!=))", rest)
        if not tail or (name == k.name and tail.group(1) != "{" and k.kind != "record"):
            continue
        prev = shallow[:m.start()].rstrip()
        if prev.endswith((".", "new")):
            continue
        sym = tail.group(1)
        b0 = close + 1 + tail.end()
        if sym == "{":
            body = (b0 - 1, match_close(p.bare, b0 - 1) + 1)
        else:
            stop = re.compile(r";|\n\s*\n|\n\s*(?:@|\[|public|private|protected|internal|fun|override)").search(p.bare, b0)
            body = (b0, stop.start() if stop else len(p.bare))
        meth = Method(name, "", line_of(p.text, start + m.start()), body, params_of(p.code[open_ + 1:close]), k.name)
        k.methods.setdefault(name, []).append(meth)
        p.methods.append(meth)


def _fields(p: Parsed, k: Klass, start: int, end: int, header: str) -> None:
    shallow = _shallow(p.bare, start, end)
    for m in JAVA_FIELD.finditer(shallow):
        if m.group(1) not in ("return", "new"):
            k.fields[m.group(2)] = m.group(1)
    for m in KOTLIN_PROP.finditer(shallow):
        k.fields[m.group(1)] = m.group(2)
    for m in KOTLIN_NEW.finditer(shallow):
        k.fields.setdefault(m.group(1), m.group(2))
    ctor = re.match(r"\s*(?:<[^{(]*?>)?\s*\(", header)  # Kotlin / C# primary constructor
    if ctor:
        hs = header.find("(")
        close = match_close(header, hs, "(", ")")
        for n, t in params_of(header[hs + 1:close]).items():
            k.fields.setdefault(n, t)
    for meths in k.methods.values():  # constructor parameters stored into fields: this.x = x / _x = x
        for meth in meths:
            if meth.name != k.name:
                continue
            body = p.bare[meth.body[0]:meth.body[1]]
            for a in re.finditer(r"(?:this\.)?(\w+)\s*=\s*(\w+)\s*[;\n]", body):
                if a.group(2) in meth.params:
                    k.fields.setdefault(a.group(1), meth.params[a.group(2)])


def parse(text: str, path: str) -> Parsed:
    p = Parsed(text, blanked(text), blanked(text, True))
    for m in CLASS_DECL.finditer(p.bare):
        name_end = m.end()
        start, end = class_span(p.bare, m.start(2))
        header = p.code[name_end:start] if end > start else ""
        k = Klass(m.group(2), path, m.group(1), span=(start, end))
        k.bases, k.base_args = _bases(header)
        k.annotations = p.code[max(0, m.start() - 400):m.start()].rsplit("}", 1)[-1].rsplit(";", 1)[-1]
        if end > start:
            _methods(p, k, start + 1, end)
            _fields(p, k, start + 1, end, header)
        p.classes.append(k)
    for meth in p.methods:
        meth.file = path
    return p


def locals_in(code: str) -> dict[str, str]:
    """Local variables with a declared or constructed type in a method body."""
    out = {}
    for m in LOCAL.finditer(code):
        if m.group(1) not in ("return", "else", "new", "throw", "await"):
            out[m.group(2)] = m.group(1)
    for rx in (VAR_NEW, KOTLIN_NEW):
        for m in rx.finditer(code):
            out[m.group(1)] = m.group(2)
    for m in KOTLIN_PROP.finditer(code):
        out[m.group(1)] = m.group(2)
    return out


def receiver_chain(bare: str, i: int) -> tuple[list[str], bool]:
    """The receiver chain before a call name at i: a.b.c.name( -> ([a, b, c], False); x().name( -> ([], True)."""
    parts: list[str] = []
    j = i
    while True:
        k = j - 1
        while k >= 0 and bare[k] in " \t\r\n":
            k -= 1
        if k < 0 or bare[k] != ".":
            break
        k -= 1
        while k >= 0 and bare[k] in "?!":
            k -= 1
        while k >= 0 and bare[k] in " \t\r\n":
            k -= 1
        if k >= 0 and bare[k] in ")]":
            return parts, True
        e = k + 1
        while k >= 0 and (bare[k].isalnum() or bare[k] == "_"):
            k -= 1
        if e == k + 1:
            break
        parts.append(bare[k + 1:e])
        j = k + 1
    parts.reverse()
    if parts and parts[0] in ("this", "base", "super"):
        parts = parts[1:]
    return parts, False


def _block_after(bare: str, i: int, kotlin: bool) -> tuple[int, int]:
    while i < len(bare) and bare[i].isspace():
        i += 1
    if i < len(bare) and bare[i] == "{":
        return i, match_close(bare, i) + 1
    stop = bare.find("\n", i) if kotlin else bare.find(";", i)
    return i, (len(bare) if stop < 0 else stop)


def regions(p: Parsed, b0: int, b1: int, kotlin: bool) -> list[tuple[int, int, int, str]]:
    """(start, end, cond, why) of every if/else/switch/when/catch block in a body."""
    bare, out = p.bare, []
    for m in re.finditer(r"\bif\s*\(", bare[b0:b1]):
        po = b0 + m.end() - 1
        pc = match_close(bare, po, "(", ")")
        why = fk.phrase(p.code[po + 1:pc])
        s, e = _block_after(bare, pc + 1, kotlin)
        out.append((s, e, 1, why))
        k = e
        while k < len(bare) and bare[k].isspace():
            k += 1
        if re.match(r"else\b(?!\s*if\b)", bare[k:k + 10]):
            other = why[:-4] + " missing" if why.endswith((" is set", " are set")) else "otherwise"
            s2, e2 = _block_after(bare, k + 4, kotlin)
            out.append((s2, e2, 1, other))
    for m in re.finditer(r"\bcatch\b\s*(?:\(([^)]*)\))?\s*\{", bare[b0:b1]):
        brace = b0 + m.end() - 1
        spec = p.code[b0 + m.start(1):b0 + m.end(1)] if m.group(1) is not None else ""
        out.append((brace, match_close(bare, brace) + 1, 2, fk.catch_text(spec)))
    for m in re.finditer(r"\b(?:switch|when)\b\s*(?:\([^)]*\))?\s*\{", bare[b0:b1]):
        brace = b0 + m.end() - 1
        out.append((brace, match_close(bare, brace) + 1, 1, ""))
    return out


def cond_at(regs: list[tuple[int, int, int, str]], pos: int) -> tuple[int, str]:
    inside = [r for r in regs if r[0] <= pos < r[1]]
    if not inside:
        return 0, ""
    innermost = min(inside, key=lambda r: r[1] - r[0])
    return max(r[2] for r in inside), innermost[3]
