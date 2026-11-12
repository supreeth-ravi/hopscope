"""Call sequences for Java, Kotlin and C# route handlers, without a parser: within the handler's body, in source order,
follow calls into methods of the service's own classes (through injected fields, parameters and locals, resolving an
interface to the class that implements it), and record the calls that leave the process: repositories and DbContexts
(sql), AWS SDK clients (aws), HTTP clients (http), Redis (cache) and Kafka producers (kafka). Calls inside an if/else,
switch or when are conditional (1) and inside a catch on error (2), as in call_flow.py for Python.

Steps use flows.json's shape: [depth, kind, label, file index, line, cond, why, via]."""
from __future__ import annotations

import re
from pathlib import Path

from langs import flowkinds as fk
from langs.common import line_of, match_close, read_contained
from langs.flowparse import KEYWORDS, Klass, Method, Parsed, cond_at, generic_args, locals_in, params_of, parse, receiver_chain, regions, simple_type

MAX_DEPTH, MAX_STEPS = 3, 28
FAMILY = {".java": "jvm", ".kt": "jvm", ".cs": "cs"}
CALL = re.compile(r"\b(\w+)\s*(?:<[^<>();]*(?:<[^<>();]*>)?[^<>();]*>)?\s*\(")
ACCESSOR = re.compile(r"^(get|set|is|has|Get|Set|Is|Has)[A-Z]")
_SERVICES: dict[tuple, "Service"] = {}


class Service:
    """Every class of one service in one language family (Java+Kotlin, or C#)."""

    def __init__(self, base: Path, files: list[str], db: str) -> None:
        self.parsed: dict[str, Parsed] = {}
        self.classes: dict[str, list[Klass]] = {}
        self.db = db
        root = base.resolve()
        for f in files:
            text = read_contained(root, f)
            if text is None:
                continue
            p = parse(text, f)
            self.parsed[f] = p
            for k in p.classes:
                self.classes.setdefault(k.name, []).append(k)

    def method_in(self, k: Klass | None, name: str, depth: int = 0) -> tuple[Klass, Method] | None:
        """A method of k or of one of its base classes."""
        if k is None or depth > 4:
            return None
        if name in k.methods and k.kind != "interface":
            return k, k.methods[name][0]
        for b in k.bases:
            for bk in self.classes.get(b, []):
                hit = self.method_in(bk, name, depth + 1)
                if hit:
                    return hit
        return None

    def implementation(self, type_name: str, meth: str) -> tuple[Klass, Method] | None:
        t = simple_type(type_name)
        for k in self.classes.get(t, []):
            hit = self.method_in(k, meth)
            if hit:
                return hit
        names = [t] + ([t[1:]] if re.match(r"I[A-Z]", t) else [])
        for ks in self.classes.values():
            for k in ks:
                if k.kind != "interface" and (t in k.bases or k.name in names[1:]):
                    hit = self.method_in(k, meth)
                    if hit:
                        return hit
        return None

    def bases_of(self, t: str, depth: int = 0) -> set[str]:
        out: set[str] = set()
        for k in self.classes.get(t, []):
            for b in k.bases:
                out.add(b)
                if depth < 4:
                    out |= self.bases_of(b, depth + 1)
        return out

    def is_dbcontext(self, t: str) -> bool:
        return bool(self.bases_of(t) & fk.DB_BASES) or (t.endswith("DbContext") and t not in self.classes)

    def is_repository(self, t: str) -> bool:
        return bool(fk.REPO_TYPE.search(t)) or any(fk.SPRING_REPOS.match(b) for b in self.bases_of(t))

    def entity(self, declared: str) -> str:
        args = generic_args(declared)
        t = simple_type(declared)
        for k in self.classes.get(t, []):
            if k.base_args:
                return k.base_args[0]
        if args:
            return args[0]
        return re.sub(r"^I(?=[A-Z])", "", re.sub(r"(Repository|Repo|Dao|DAO)$", "", t))

    def is_client(self, t: str) -> bool:
        return any(k.kind == "interface" and re.search(r"@FeignClient|@Client\b|@HttpExchange", k.annotations) for k in self.classes.get(t, []))

    def klass_at(self, path: str, pos: int) -> Klass | None:
        """The innermost class whose body contains pos."""
        inside = [k for k in self.parsed[path].classes if k.span[0] <= pos < k.span[1]]
        return min(inside, key=lambda k: k.span[1] - k.span[0], default=None)


class Tracer:
    def __init__(self, svc: Service, kotlin_files: set[str]) -> None:
        self.svc = svc
        self.kotlin = kotlin_files
        self.steps: list[list] = []
        self.seen: set[tuple] = set()

    def types_for(self, k: Klass | None, meth: Method) -> dict[str, str]:
        types: dict[str, str] = {}
        chain, seen = [k] if k else [], set()
        while chain:  # fields of the class and its base classes
            c = chain.pop()
            if c.name in seen:
                continue
            seen.add(c.name)
            for n, t in c.fields.items():
                types.setdefault(n, t)
            chain += [bk for b in c.bases for bk in self.svc.classes.get(b, [])]
        p = self.svc.parsed[meth.file]
        types.update(meth.params)
        types.update(locals_in(p.code[meth.body[0]:meth.body[1]]))
        return types

    def add(self, depth: int, kind: str, label: str, path: str, pos: int, cond: int, why: str, via: str) -> None:
        line = line_of(self.svc.parsed[path].text, pos)
        step = [depth, kind, label, path, line, cond, why, via]
        if not self.steps or self.steps[-1] != step:  # one statement, one step
            self.steps.append(step)

    def run(self, meth: Method, k: Klass | None, depth: int = 0, inherited: int = 0, inherited_why: str = "") -> None:
        if depth > MAX_DEPTH or len(self.steps) >= MAX_STEPS:
            return
        p = self.svc.parsed[meth.file]
        b0, b1 = meth.body
        types = self.types_for(k, meth)
        regs = regions(p, b0, b1, meth.file in self.kotlin)
        via = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", re.sub(r"Async$", "", meth.name)).lower() if depth else ""
        for m in CALL.finditer(p.bare, b0, b1):
            name, pos = m.group(1), m.start(1)
            if name in KEYWORDS or re.search(r"\bnew\s*$", p.bare[max(0, pos - 8):pos]) or len(self.steps) >= MAX_STEPS:
                continue
            chain, broken = receiver_chain(p.bare, pos)
            if broken:
                continue
            open_ = m.end() - 1
            close = match_close(p.bare, open_, "(", ")")
            args, after = p.code[open_ + 1:close], p.code[close + 1:close + 300]
            cond, why = cond_at(regs, pos)
            cond, why = max(cond, inherited), why or inherited_why
            self.call(chain, name, args, after, types, k, meth, depth, pos, cond, why, via)

    def call(self, chain, name, args, after, types, k, meth, depth, pos, cond, why, via) -> None:
        path = meth.file
        if not chain:
            hit = self.svc.method_in(k, name)
            if hit:
                self.follow(hit, depth, cond, why, via)
            return
        root = chain[0]
        declared = types.get(root) or (root if root[:1].isupper() and root in self.svc.classes else None)
        if declared is None:
            return
        t = simple_type(declared)
        db = fk.DB_LABEL.get(self.svc.db, "Postgres")
        if len(chain) >= 2 and self.svc.is_dbcontext(t):
            self.add(depth, "sql", f"{db}: {fk.sql_verb(name) or 'query'} {chain[1]}".rstrip(), path, pos, cond, why, via)
            return
        if len(chain) != 1:
            return
        kind = self.external(t, declared, name, args, after, db)
        if kind:
            if kind[0] == "skip":
                return
            if kind[0] == "commit":
                last = self.steps[-1] if self.steps else None
                if last and last[1] == "sql" and last[0] == depth:
                    last[2] += "" if "commit" in last[2] else " + commit"
                    return
                kind = ("sql", f"{db}: commit")
            self.add(depth, kind[0], kind[1], path, pos, cond, why, via)
            return
        hit = self.svc.implementation(t, name)
        if hit:
            self.follow(hit, depth, cond, why, via)
        elif self.svc.is_repository(t):
            self.add(depth, "sql", f"{db}: {fk.sql_verb(name) or 'query'} {self.svc.entity(declared)}".rstrip(), path, pos, cond, why, via)

    def external(self, t: str, declared: str, name: str, args: str, after: str, db: str) -> tuple[str, str] | None:
        svc = fk.aws_service(t)
        if svc:
            api = "SendMessage" if svc == "SQS" and name in ("send", "sendAsync", "convertAndSend") else fk.api_name(name)
            return fk.STEP_KIND.get(svc, "aws"), f"{svc}: {api}"
        if fk.KAFKA_TYPE.match(t) and re.match(r"(send|produce|publish)", name, re.I):
            topic = fk.strings_in(args)
            return "kafka", f"Kafka: send to {topic[0]}" if topic else "Kafka: send"
        if fk.CACHE_TYPE.match(t):
            return "cache", f"Cache: {fk.cache_verb(name, after)}"
        if fk.HTTP_TYPE.match(t):
            verb, target = fk.http_call(name, args, after)
            return "http", f"HTTP {verb} {target}"
        if fk.SQL_TYPE.match(t):
            label = fk.sql_label(name, args)
            return ("skip", "") if label is None else ("sql", f"{db}: {label}")
        if self.svc.is_dbcontext(t):
            return ("commit", "") if name.startswith("SaveChanges") else ("sql", f"{db}: {fk.sql_verb(name) or 'query'}")
        if self.svc.is_client(t):
            return "http", f"HTTP {self.client_route(t, name)}"
        return None

    def client_route(self, t: str, name: str) -> str:
        """The method and path a Feign / Micronaut client interface method calls."""
        for k in self.svc.classes.get(t, []):
            text = self.svc.parsed[k.file].code[k.span[0]:k.span[1]]
            m = re.search(r"@(Get|Post|Put|Delete|Patch)(?:Mapping)?\(\s*(?:value\s*=\s*|path\s*=\s*)?\"([^\"]*)\"[^)]*\)[^;{]*?\b" + re.escape(name) + r"\s*\(", text)
            if m:
                return f"{m.group(1).upper()} {m.group(2)}"
        return "(other service)"

    def follow(self, hit: tuple[Klass, Method], depth: int, cond: int, why: str, via: str) -> None:
        k, target = hit
        key = (target.file, target.name, target.line)
        if key in self.seen:
            return
        self.seen.add(key)
        self.steps.append([depth, "call", f"{target.name}()", target.file, target.line, cond, why, via])
        before = len(self.steps)
        self.run(target, k, depth + 1, cond, why)
        if len(self.steps) == before and (depth >= 1 or ACCESSOR.match(target.name)):
            self.steps.pop()  # drop leaf helpers and getters/setters that do nothing external


def _service_of(path: str, services: list[str]) -> str:
    for s in sorted(services, key=len, reverse=True):
        if s == "." or path.startswith(s + "/"):
            return s
    return "."


def _db_of(files: list[dict]) -> str | None:
    kinds = [x for f in files for x in f.get("x", []) if x in ("postgres", "mysql", "mssql", "mongo")]
    return max(set(kinds), key=kinds.count) if kinds else None


def service_for(data: dict, base: Path, path: str) -> Service:
    services = data.get("services") or ["."]
    sid, fam = _service_of(path, services), FAMILY.get(Path(path).suffix, "")
    key = (str(base), sid, fam, len(data.get("files", [])))
    if key not in _SERVICES:
        files = [f for f in data.get("files", []) if FAMILY.get(Path(f["p"]).suffix) == fam and _service_of(f["p"], services) == sid]
        db = _db_of(files) or _db_of(data.get("files", [])) or "postgres"  # a service whose data layer is a shared library
        _SERVICES[key] = Service(base, [f["p"] for f in files], db)
    return _SERVICES[key]


def _offset(text: str, line: int) -> int:
    pos = 0
    for _ in range(line - 1):
        nxt = text.find("\n", pos)
        if nxt < 0:
            return len(text)
        pos = nxt + 1
    return pos


def _lambda_handler(svc: Service, path: str, start: int) -> tuple[Method, Klass | None] | None:
    """The handler of a minimal API (app.MapGet("/x", (A a) => {...}) or a method group) or a Ktor get("/x") { } block
    that starts on the route's line."""
    p = svc.parsed[path]
    line_end = p.bare.find("\n", start)
    line_end = len(p.bare) if line_end < 0 else line_end
    k = svc.klass_at(path, start)
    ktor = re.compile(r"\b(?:get|post|put|delete|patch|head|options)\s*(?:\([^)]*\))?\s*\{").search(p.bare, start, line_end)
    mapc = re.compile(r"\bMap\w*\s*\(").search(p.bare, start, line_end)
    if mapc:
        open_ = mapc.end() - 1
        close = match_close(p.bare, open_, "(", ")")
        arrow = p.bare.find("=>", open_, close)
        if arrow < 0:
            group = re.search(r",\s*(?:\w+\.)?(\w+)\s*$", p.bare[open_ + 1:close])
            if not group:
                return None
            hit = svc.method_in(k, group.group(1)) or next((svc.method_in(c, group.group(1)) for c in p.classes if group.group(1) in c.methods), None)
            return (hit[1], hit[0]) if hit else None
        head = p.bare[open_:arrow].rstrip()
        params = {}
        if head.endswith(")"):
            depth, i = 0, len(head) - 1
            while i > 0:
                depth += {")": 1, "(": -1}.get(head[i], 0)
                if depth == 0:
                    break
                i -= 1
            params = params_of(p.code[open_ + i + 1:open_ + len(head) - 1])
        b = arrow + 2
        while b < close and p.bare[b].isspace():
            b += 1
        body = (b, match_close(p.bare, b) + 1) if p.bare[b:b + 1] == "{" else (b, close)
        return Method("handler", path, line_of(p.text, start), body, params, k.name if k else ""), k
    if ktor:
        brace = ktor.end() - 1
        return Method("handler", path, line_of(p.text, start), (brace, match_close(p.bare, brace) + 1), {}, k.name if k else ""), k
    return None


def handler(svc: Service, path: str, line: int) -> tuple[Method, Klass | None] | None:
    p = svc.parsed.get(path)
    if p is None:
        return None
    near = sorted((m for m in p.methods if 0 <= m.line - line <= 3), key=lambda m: m.line - line)
    if near:
        m = near[0]
        return m, next((k for k in p.classes if m.name in k.methods and m in k.methods[m.name]), None)
    return _lambda_handler(svc, path, _offset(p.text, line))


def flows(data: dict, base: Path, route_index: int) -> list[list] | None:
    """The call sequence of one Java/Kotlin/C# route handler, or None when its handler can't be found."""
    files = [f["p"] for f in data.get("files", [])]
    r = data["routes"][route_index]
    path = files[r["f"]]
    svc = service_for(data, base, path)
    found = handler(svc, path, int(r.get("l") or 0))
    if not found:
        return None
    meth, k = found
    t = Tracer(svc, {f for f in svc.parsed if f.endswith(".kt")})
    t.seen.add((meth.file, meth.name, meth.line))
    t.run(meth, k)
    idx = {p: i for i, p in enumerate(files)}
    return [[d, kind, lab, idx.get(p, -1), ln, c, why, via] for d, kind, lab, p, ln, c, why, via in t.steps]
