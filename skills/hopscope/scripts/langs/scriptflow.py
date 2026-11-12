"""Call sequences for Ruby, PHP and Rust route handlers, without a parser. A language supplies a small adapter (parse a
file into functions and classes, list the calls in a body, say which call leaves the process and which one reaches
the service's own code); this module walks the handler body in source order, records external calls (sql, cache, aws,
http, kafka, workflow, stream), follows calls into the service's own functions one level deep, and marks calls inside
a branch as conditional (1) and inside an error handler as on-error (2), as braceflow.py does for Java/Kotlin/C#.

Steps use flows.json's shape: [depth, kind, label, file index, line, cond, why, via]."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from langs.common import line_of, read_contained

MAX_STEPS = 28
DB_LABEL = {"postgres": "Postgres", "mysql": "MySQL", "mssql": "SQL Server", "mongo": "MongoDB"}
_SERVICES: dict[tuple, "Service"] = {}


@dataclass
class Fn:
    name: str
    file: str
    line: int
    body: tuple[int, int]    # [start, end) in the file text
    owner: str = ""          # class / module / impl type
    static: bool = False     # def self.x, static function, associated fn without self


@dataclass
class Klass:
    name: str
    file: str
    bases: list[str] = field(default_factory=list)
    fns: dict[str, list[Fn]] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)   # property / injected dependency -> type


@dataclass
class Unit:
    text: str
    code: str       # comments blanked, strings kept
    bare: str       # comments and string contents blanked
    fns: list[Fn] = field(default_factory=list)
    classes: list[Klass] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # what one language keeps per file (e.g. Ruby's blocks)


@dataclass
class Call:
    pos: int
    chain: list[str]  # receivers before the name: a.b.name( -> [a, b]
    name: str
    args: str         # argument text, strings kept


Region = tuple[int, int, int, str]


class Adapter(Protocol):
    def parse(self, text: str, path: str) -> Unit: ...
    def calls(self, u: Unit, fn: Fn) -> list[Call]: ...
    def regions(self, u: Unit, fn: Fn) -> list[Region]: ...
    def external(self, svc: "Service", u: Unit, fn: Fn, c: Call, types: dict[str, str]) -> tuple[str, str] | None: ...
    def resolve(self, svc: "Service", u: Unit, fn: Fn, c: Call, types: dict[str, str]) -> Fn | None: ...
    def local_types(self, svc: "Service", u: Unit, fn: Fn) -> dict[str, str]: ...
    def inline(self, u: Unit, path: str, line: int) -> Fn | None: ...
    def accessor(self, name: str) -> bool: ...


class Service:
    """Every file of one service in one language, parsed."""

    def __init__(self, base: Path, files: list[str], db: str, lang: Adapter) -> None:
        self.units: dict[str, Unit] = {}
        self.classes: dict[str, list[Klass]] = {}
        self.free: dict[str, list[Fn]] = {}
        self.db = db
        self.extra: dict[str, Any] = {}
        root = base.resolve()
        for f in files:
            text = read_contained(root, f)
            if text is None:
                continue
            u = lang.parse(text, f)
            self.units[f] = u
            for k in u.classes:
                self.classes.setdefault(k.name, []).append(k)
            for fn in u.fns:
                if not fn.owner:
                    self.free.setdefault(fn.name, []).append(fn)

    def method_in(self, cls: str, name: str, depth: int = 0) -> Fn | None:
        """A method of class cls or of one of its base classes."""
        if depth > 4:
            return None
        for k in self.classes.get(cls, []):
            if name in k.fns:
                return k.fns[name][0]
            for b in k.bases:
                hit = self.method_in(b, name, depth + 1)
                if hit:
                    return hit
        return None

    def bases_of(self, cls: str, depth: int = 0) -> set[str]:
        out: set[str] = set()
        for k in self.classes.get(cls, []):
            for b in k.bases:
                out.add(b)
                if depth < 6:
                    out |= self.bases_of(b, depth + 1)
        return out


def service_of(path: str, services: list[str]) -> str:
    for s in sorted(services, key=len, reverse=True):
        if s == "." or path.startswith(s + "/"):
            return s
    return "."


def db_of(files: list[dict]) -> str | None:
    kinds = [x for f in files for x in f.get("x", []) if x in DB_LABEL]
    return max(set(kinds), key=kinds.count) if kinds else None


def service_for(data: dict, base: Path, path: str, lang: Adapter, suffixes: tuple[str, ...], default_db: str = "Postgres") -> Service:
    services = data.get("services") or ["."]
    sid = service_of(path, services)
    key = (str(base), sid, suffixes, len(data.get("files", [])))
    if key not in _SERVICES:
        files = [f for f in data.get("files", []) if f["p"].endswith(suffixes) and service_of(f["p"], services) == sid]
        kind = db_of(files)
        _SERVICES[key] = Service(base, [f["p"] for f in files], DB_LABEL[kind] if kind else default_db, lang)
        _SERVICES[key].extra["sid"] = sid
    return _SERVICES[key]


def cond_at(regs: list[Region], pos: int) -> tuple[int, str]:
    inside = [r for r in regs if r[0] <= pos < r[1]]
    if not inside:
        return 0, ""
    innermost = min(inside, key=lambda r: r[1] - r[0])
    return max(r[2] for r in inside), innermost[3]


class Tracer:
    def __init__(self, svc: Service, lang: Adapter) -> None:
        self.svc, self.lang = svc, lang
        self.steps: list[list] = []
        self.seen: set[tuple] = set()

    def add(self, depth: int, kind: str, label: str, path: str, pos: int, cond: int, why: str, via: str) -> None:
        step = [depth, kind, label, path, line_of(self.svc.units[path].text, pos), cond, why, via]
        if not self.steps or self.steps[-1] != step:  # one statement, one step
            self.steps.append(step)

    def run(self, fn: Fn, depth: int = 0, inherited: int = 0, inherited_why: str = "") -> None:
        u = self.svc.units[fn.file]
        types = self.lang.local_types(self.svc, u, fn)
        regs = self.lang.regions(u, fn)
        via = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", fn.name).replace("_", " ").strip().lower() if depth else ""
        for c in self.lang.calls(u, fn):
            if len(self.steps) >= MAX_STEPS:
                return
            cond, why = cond_at(regs, c.pos)
            cond, why = max(cond, inherited), why or inherited_why
            ext = self.lang.external(self.svc, u, fn, c, types)
            if ext:
                if ext[0] != "skip":
                    self.add(depth, ext[0], ext[1], fn.file, c.pos, cond, why, via)
                continue
            if depth >= 1:
                continue  # same-service calls are followed one level deep
            target = self.lang.resolve(self.svc, u, fn, c, types)
            if target is not None:
                self.follow(target, depth, cond, why, via)

    def follow(self, target: Fn, depth: int, cond: int, why: str, via: str) -> None:
        key = (target.file, target.name, target.line)
        if key in self.seen:
            return
        self.seen.add(key)
        self.steps.append([depth, "call", f"{target.name}()", target.file, target.line, cond, why, via])
        before = len(self.steps)
        self.run(target, depth + 1, cond, why)
        if len(self.steps) == before and self.lang.accessor(target.name):
            self.steps.pop()  # predicates and getters that do nothing external


def handler(svc: Service, lang: Adapter, path: str, line: int) -> Fn | None:
    u = svc.units.get(path)
    if u is None:
        return None
    near = sorted((f for f in u.fns if 0 <= f.line - line <= 3), key=lambda f: f.line - line)
    return near[0] if near else lang.inline(u, path, line)


def flows(lang: Adapter, suffixes: tuple[str, ...], data: dict, base: Path, route_index: int, default_db: str = "Postgres") -> list[list] | None:
    """The call sequence of one route handler, or None when its handler can't be found."""
    files = [f["p"] for f in data.get("files", [])]
    r = data["routes"][route_index]
    path = files[r["f"]]
    svc = service_for(data, base, path, lang, suffixes, default_db)
    fn = handler(svc, lang, path, int(r.get("l") or 0))
    if fn is None:
        return None
    t = Tracer(svc, lang)
    t.seen.add((fn.file, fn.name, fn.line))
    t.run(fn)
    idx = {p: i for i, p in enumerate(files)}
    return [[d, kind, lab, idx.get(p, -1), ln, c, why, via] for d, kind, lab, p, ln, c, why, via in t.steps]


SQL_WRITE = (("INSERT", re.compile(r"\binsert\s+into\s+([\w.\"`]+)", re.I)), ("UPDATE", re.compile(r"\bupdate\s+([\w.\"`]+)\s+set\b", re.I)),
             ("DELETE", re.compile(r"\bdelete\s+from\s+([\w.\"`]+)", re.I)))
SQL_FROM = re.compile(r"\bfrom\s+([\w.\"`]+)", re.I)


def sql_text_label(sql: str) -> str | None:
    """What a SQL statement does to which table: the first write (INSERT article), else SELECT <first table>."""
    for verb, rx in SQL_WRITE:
        m = rx.search(sql)
        if m:
            return f"{verb} {m.group(1).strip(chr(34) + '`')}"
    if not re.search(r"\bselect\b", sql, re.I):
        return None
    froms = [(sql.count("(", 0, m.start()) - sql.count(")", 0, m.start()), m.group(1)) for m in SQL_FROM.finditer(sql)]
    if not froms:
        return None
    top = min(d for d, _ in froms)  # the statement's own FROM, not a subquery's
    return f"SELECT {next(t for d, t in froms if d == top).strip(chr(34) + '`')}"
