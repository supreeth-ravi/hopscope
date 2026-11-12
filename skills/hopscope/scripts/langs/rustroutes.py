"""Rust routes, read from one file: axum `.route("/x", get(h).post(h2))` under `.nest("/p", …)` (an inline router, a
local `let` router, or a fn returning one: same file here, another file through `Nest.target`), actix-web
`.route("/x", web::get().to(h))`, `web::resource("/x").route(web::get().to(h))` and `web::scope("/p")` (including the
attribute-routed fns registered with `.service(f)` inside it), Rocket `mount("/p", routes![a, b])`, and the
`#[get("/x")]` attribute macros of actix-web and Rocket. Paths keep the framework's own parameter syntax."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from langs.common import join_path, line_of, match_close
from langs.rails import split
from langs.rusttext import blank, enclosing_paren, fn_spans, literals, statement_spans

AXUM_VERB = re.compile(r"\b(get|post|put|patch|delete|head|options|trace|any)\s*\(")
ACTIX_TO = re.compile(r"web\s*::\s*(get|post|put|patch|delete|head|trace)\s*\(\s*\)\s*\.\s*to(?:_async)?\s*\(\s*([\w:]+)")
ATTR = re.compile(r"#\[\s*(?:\w+::)?(get|post|put|patch|delete|head|options)\s*\(\s*\"([^\"]*)\"")
ATTR_ROUTE = re.compile(r"#\[\s*(?:\w+::)?route\s*\(\s*\"([^\"]*)\"([^\]]*)\]")


@dataclass
class Found:
    verb: str
    path: str
    handler: str   # routes::parcels::list, a fn name, or "" (closure)
    line: int
    pos: int


@dataclass
class Nest:
    segs: list[str]  # the fn that builds the nested router: routes::admin::router -> [routes, admin, router]
    prefix: str


@dataclass
class FileRoutes:
    routes: list[Found] = field(default_factory=list)
    attrs: list[Found] = field(default_factory=list)          # #[get("/x")] fns: handler is the fn's own name
    scoped: dict[str, list[str]] = field(default_factory=dict)  # fn name -> prefixes it is registered under (actix scope, rocket mount)
    nests: list[Nest] = field(default_factory=list)            # routers built by a fn elsewhere, nested under a prefix


class Prefixes:
    """Path prefixes from axum `.nest` calls in one file: inline routers, `let` routers and same-file router fns."""

    def __init__(self, bare: str, code: str) -> None:
        self.inline: list[tuple[int, int, int, str]] = []      # (nest pos, span start, span end, path)
        self.by_var: dict[str, tuple[int, str]] = {}
        self.by_fn: dict[str, tuple[int, str]] = {}
        self.calls: list[tuple[int, list[str], str]] = []      # (nest pos, fn segs, path) for routers built elsewhere
        self.lets = statement_spans(bare)
        self.fns = {name: (b0, b1) for name, _, b0, b1 in fn_spans(bare)}
        for m in re.finditer(r"\.\s*nest\s*\(", bare):
            open_ = m.end() - 1
            close = match_close(bare, open_, "(", ")")
            args = split(code[open_ + 1:close])
            path = literals(args[0])[0] if args and literals(args[0]) else ""
            rest = args[1].strip() if len(args) > 1 else ""
            call = re.fullmatch(r"([\w:]+)\s*\(\s*\)", rest)
            if re.fullmatch(r"\w+", rest):
                self.by_var[rest] = (m.start(), path)
            elif call:
                segs = [s for s in call.group(1).split("::") if s]
                if len(segs) == 1 and segs[0] in self.fns:
                    self.by_fn[segs[0]] = (m.start(), path)
                else:
                    self.calls.append((m.start(), segs, path))
            else:
                self.inline.append((m.start(), open_, close, path))

    def at(self, pos: int, depth: int = 0) -> str:
        if depth > 12:
            return ""
        inl = [s for s in self.inline if s[1] < pos < s[2]]
        if inl:
            nest_pos, _, _, path = max(inl, key=lambda s: s[1])
            return join_path(self.at(nest_pos, depth + 1), path)
        lets = [s for s in self.lets if s[1] <= pos < s[2] and s[0] in self.by_var]
        if lets:
            nest_pos, path = self.by_var[max(lets, key=lambda s: s[1])[0]]
            return join_path(self.at(nest_pos, depth + 1), path)
        for name, (b0, b1) in self.fns.items():
            if b0 < pos < b1 and name in self.by_fn:
                nest_pos, path = self.by_fn[name]
                return join_path(self.at(nest_pos, depth + 1), path)
        return ""


def _axum(code: str, bare: str, text: str, pre: Prefixes) -> list[Found]:
    out = []
    for m in re.finditer(r"\.\s*route\s*\(", bare):
        open_ = m.end() - 1
        close = match_close(bare, open_, "(", ")")
        args = split(code[open_ + 1:close])
        lits = literals(args[0]) if args else []
        if not lits or len(args) < 2 or not args[0].strip().startswith('"'):
            continue
        base = join_path(pre.at(m.start()), lits[0])
        line = line_of(text, m.start())
        target = args[1]
        if "web::" in target or re.search(r"\.\s*to(?:_async)?\s*\(", target):
            for verb, h in ACTIX_TO.findall(target):
                out.append(Found(verb.upper(), base, h, line, m.start()))
            continue
        tbare = blank(target, True)
        end = -1
        for v in AXUM_VERB.finditer(tbare):
            if v.start() < end:
                continue
            vclose = match_close(tbare, v.end() - 1, "(", ")")
            end = vclose
            h = target[v.end():vclose].strip()
            out.append(Found(v.group(1).upper(), base, h if re.fullmatch(r"[\w:]+", h) else "", line, m.start()))
    return out


def _scopes(bare: str, code: str) -> list[tuple[int, int, str]]:
    out = []
    for m in re.finditer(r"web\s*::\s*scope\s*\(", bare):
        close = match_close(bare, m.end() - 1, "(", ")")
        lits = literals(code[m.end():close])
        out.append((m.start(), enclosing_paren(bare, m.start()), lits[0] if lits else ""))
    return out


def _scope_at(scopes: list[tuple[int, int, str]], pos: int) -> str:
    return join_path("", "/".join(p.strip("/") for s, e, p in sorted(scopes) if s <= pos < e))


def _actix(code: str, bare: str, text: str, scopes) -> tuple[list[Found], dict[str, list[str]]]:
    out = []
    for m in re.finditer(r"web\s*::\s*resource\s*\(", bare):
        close = match_close(bare, m.end() - 1, "(", ")")
        lits = literals(code[m.end():close])
        if not lits:
            continue
        span_end = enclosing_paren(bare, m.start())
        chain = code[close + 1:span_end]
        path = join_path(_scope_at(scopes, m.start()), lits[0])
        found = ACTIX_TO.findall(chain)
        direct = re.match(r"\s*\.\s*to(?:_async)?\s*\(\s*([\w:]+)", chain)
        for verb, h in found:
            out.append(Found(verb.upper(), path, h, line_of(text, m.start()), m.start()))
        if direct and not found:
            out.append(Found("ANY", path, direct.group(1), line_of(text, m.start()), m.start()))
    scoped: dict[str, list[str]] = {}
    for m in re.finditer(r"\.\s*service\s*\(\s*([\w:]+)\s*\)", bare):
        scoped.setdefault(m.group(1).split("::")[-1], []).append(_scope_at(scopes, m.start()))
    for m in re.finditer(r"\.\s*mount\s*\(\s*\"([^\"]*)\"\s*,\s*routes!\s*\[([^\]]*)\]", code):
        for name in re.findall(r"[\w:]+", m.group(2)):
            scoped.setdefault(name.split("::")[-1], []).append(m.group(1))
    return out, scoped


def _attrs(code: str, text: str) -> list[Found]:
    out = []
    for m in ATTR.finditer(code):
        fn = re.compile(r"\bfn\s+(\w+)").search(code, m.end())
        if fn:
            out.append(Found(m.group(1).upper(), m.group(2), fn.group(1), line_of(text, fn.start(1)), fn.start(1)))
    for m in ATTR_ROUTE.finditer(code):
        fn = re.compile(r"\bfn\s+(\w+)").search(code, m.end())
        verbs = re.findall(r"method\s*=\s*\"(\w+)\"", m.group(2)) or ["ANY"]
        if fn:
            out += [Found(v.upper(), m.group(1), fn.group(1), line_of(text, fn.start(1)), fn.start(1)) for v in verbs]
    return out


def parse(text: str) -> FileRoutes:
    code, bare = blank(text), blank(text, True)
    out = FileRoutes()
    pre = Prefixes(bare, code)
    scopes = _scopes(bare, code)
    if ".route" in bare:
        out.routes = _axum(code, bare, text, pre)
        for r in out.routes:
            r.path = join_path(_scope_at(scopes, r.pos), r.path) if scopes else r.path
    actix, out.scoped = _actix(code, bare, text, scopes)
    out.routes += actix
    out.attrs = _attrs(code, text)
    out.nests = [Nest(segs, join_path(pre.at(pos), path)) for pos, segs, path in pre.calls]
    return out
