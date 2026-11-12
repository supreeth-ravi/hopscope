"""PHP routes: Laravel route files (Route::get/post/put/patch/delete/options/any/match/view, resource/apiResource with
only/except, Route::prefix('x')->group(...) and Route::group(['prefix' => 'x'], ...), middleware ignored), Symfony
#[Route] attributes and @Route annotations with a class-level prefix, and Slim / Lumen `$app->get('/x', ...)` with
`$app->group('/x', ...)`. Each route names its controller class and method when it has one.

Laravel's resource routes are the framework's: `resource` includes create and edit (HTML forms) unless only/except
drop them, `apiResource` does not; update is both PUT and PATCH."""
from __future__ import annotations

import re
from dataclasses import dataclass

from langs.common import join_path, line_of, match_close
from langs.phptext import blank, literals
from langs.rails import split
from langs.rubytext import singular

VERBS = ("get", "post", "put", "patch", "delete", "options")
LARAVEL = re.compile(r"(?:\bRoute::|->)\s*(get|post|put|patch|delete|options|any|match|view|resource|apiResource|resources|apiResources|group)\s*\(")
RESOURCE = (("index", "GET", ""), ("create", "GET", "/create"), ("store", "POST", ""), ("show", "GET", "/{p}"), ("edit", "GET", "/{p}/edit"),
            ("update", "PUT", "/{p}"), ("update", "PATCH", "/{p}"), ("destroy", "DELETE", "/{p}"))
TARGET_ARRAY = re.compile(r"\[\s*\\?([\w\\]+)::class\s*,\s*['\"](\w+)['\"]\s*\]")
TARGET_AT = re.compile(r"['\"]\\?([\w\\]+)@(\w+)['\"]")
TARGET_CLASS = re.compile(r"^\s*\\?([\w\\]+)::class\s*$")
CLOSURE = re.compile(r"\b(?:static\s+)?function\s*\([^)]*\)\s*(?:use\s*\([^)]*\)\s*)?(?::\s*\??[\w\\]+\s*)?\{")
SLIM = re.compile(r"\$(\w+)\s*->\s*(get|post|put|patch|delete|options|any|map|group)\s*\(")


@dataclass
class Route:
    verb: str
    path: str
    cls: str       # controller class as written (LoafController, \App\Http\X, Api\X) or ""
    action: str
    line: int
    ns: str = ""   # group namespace for string targets ('Api')


def _chain_start(bare: str, pos: int) -> int:
    k = max(bare.rfind(";", 0, pos), bare.rfind("{", 0, pos), bare.rfind("}", 0, pos), bare.rfind("<?php", 0, pos) + 4)
    k += 1
    while k < pos and bare[k].isspace():
        k += 1
    return k


def _target(arg: str, controller: str) -> tuple[str, str]:
    for rx in (TARGET_ARRAY, TARGET_AT):
        m = rx.search(arg)
        if m:
            return m.group(1), m.group(2)
    m = TARGET_CLASS.match(arg)
    if m:
        return m.group(1), "__invoke"
    lit = re.fullmatch(r"\s*['\"](\w+)['\"]\s*", arg)
    if lit and controller:
        return controller, lit.group(1)
    return "", ""


def _attrs(code: str, chain: str, args: list[str]) -> tuple[str, str, str]:
    """(prefix, namespace, controller) a group sets, from its fluent chain or its attribute array."""
    arr = args[0] if args and args[0].strip().startswith("[") else ""

    def pick(key: str) -> str:
        m = re.search(rf"\b{key}\s*\(\s*['\"]([^'\"]*)['\"]", chain) or re.search(rf"['\"]{key}['\"]\s*=>\s*['\"]([^'\"]*)['\"]", arr)
        return m.group(1) if m else ""
    ctrl = re.search(r"\bcontroller\s*\(\s*\\?([\w\\]+)::class", chain) or re.search(r"['\"]controller['\"]\s*=>\s*\\?([\w\\]+)::class", arr)
    return pick("prefix"), pick("namespace"), ctrl.group(1) if ctrl else ""


def _resource(kind: str, args: list[str], after: str, prefix: str, line: int, ns: str) -> list[Route]:
    lits = literals(args[0]) if args else []
    if not lits or len(args) < 2:
        return []
    name = lits[0].strip("/")
    m = re.match(r"\s*\\?([\w\\]+)::class|\s*['\"]([\w\\]+)['\"]", args[1])
    cls = (m.group(1) or m.group(2)) if m else ""
    opts = args[2] if len(args) > 2 else ""
    only = re.search(r"->\s*only\s*\(([^)]*)\)|['\"]only['\"]\s*=>\s*(\[[^\]]*\])", after + " " + opts)
    exc = re.search(r"->\s*except\s*\(([^)]*)\)|['\"]except['\"]\s*=>\s*(\[[^\]]*\])", after + " " + opts)
    only_l = literals(only.group(1) or only.group(2)) if only else None
    exc_l = literals(exc.group(1) or exc.group(2)) if exc else []
    segs = name.split(".")
    path = "/".join(f"{s}/{{{singular(s).replace('-', '_')}}}" for s in segs[:-1]) + ("/" if len(segs) > 1 else "") + segs[-1]
    param = singular(segs[-1].split("/")[-1]).replace("-", "_")
    out = []
    for action, verb, tail in RESOURCE:
        if kind.startswith("api") and action in ("create", "edit"):
            continue
        if (only_l is not None and action not in only_l) or action in exc_l:
            continue
        out.append(Route(verb, join_path(prefix, path + tail.replace("{p}", "{" + param + "}")), cls, action, line, ns))
    return out


def laravel(text: str, api: bool) -> list[Route]:
    code, bare = blank(text), blank(text, True)
    groups: list[tuple[int, int, str, str, str]] = []
    regs: list[tuple[int, str, list[str], str, int]] = []
    for m in LARAVEL.finditer(bare):
        start = _chain_start(bare, m.start())
        if not bare.startswith("Route::", start):
            continue
        open_ = m.end() - 1
        close = match_close(bare, open_, "(", ")")
        args = split(code[open_ + 1:close])
        if m.group(1) == "group":
            body = CLOSURE.search(bare, open_, close)
            if body:
                brace = body.end() - 1
                groups.append((brace, match_close(bare, brace), *_attrs(code, code[start:m.start()], args)))
            continue
        end = bare.find(";", close)
        regs.append((m.start(), m.group(1), args, code[close + 1:end if end >= 0 else close + 200], start))
    out: list[Route] = []
    for pos, kind, args, after, start in regs:
        around = sorted((g for g in groups if g[0] < pos < g[1]), key=lambda g: g[0])
        prefix = join_path("/api" if api else "", "/".join(g[2] for g in around)) if around or api else ""
        ns = "\\".join(g[3] for g in around if g[3])
        ctrl = next((g[4] for g in reversed(around) if g[4]), "")
        line = line_of(text, start)
        if kind in ("resource", "apiResource"):
            out += _resource(kind, args, after, prefix, line, ns)
            continue
        if kind in ("resources", "apiResources"):
            for name, cls in re.findall(r"['\"]([\w./{}-]+)['\"]\s*=>\s*\\?([\w\\]+)::class", args[0] if args else ""):
                out += _resource(kind[:-1], [f"'{name}'", f"{cls}::class"], after, prefix, line, ns)
            continue
        if kind == "match":
            verbs = [v.upper() for v in literals(args[0])] if args else []
            path_arg, handler = (args[1] if len(args) > 1 else ""), (args[2] if len(args) > 2 else "")
        else:
            verbs = ["GET" if kind == "view" else kind.upper()]
            path_arg, handler = (args[0] if args else ""), (args[1] if len(args) > 1 else "")
        lits = literals(path_arg)
        if not lits:
            continue
        cls, action = _target(handler, ctrl) if kind != "view" else ("", "")
        out += [Route(v, join_path(prefix, lits[0]), cls, action, line, ns) for v in verbs]
    return out


def slim(text: str) -> list[Route]:
    """Slim `$app->get('/x', h)` and Lumen `$router->get('x', 'C@m')`, under `->group('/p', fn)` / group(['prefix' => 'p'])."""
    code, bare = blank(text), blank(text, True)
    groups: list[tuple[int, int, str]] = []
    regs = []
    for m in SLIM.finditer(bare):
        open_ = m.end() - 1
        close = match_close(bare, open_, "(", ")")
        args = split(code[open_ + 1:close])
        if m.group(2) == "group":
            body = CLOSURE.search(bare, open_, close)
            lit = literals(args[0]) if args and not args[0].strip().startswith("[") else []
            pre = lit[0] if lit else _attrs(code, "", args)[0]
            if body:
                brace = body.end() - 1
                groups.append((brace, match_close(bare, brace), pre))
            continue
        regs.append((m, args))
    out = []
    for m, args in regs:
        verbs = [m.group(2).upper()]
        if m.group(2) == "map":
            verbs, args = [v.upper() for v in literals(args[0])] if args else [], args[1:]
        lits = literals(args[0]) if args else []
        if not lits or not (lits[0].startswith("/") or m.group(1) == "router"):
            continue
        prefix = "/".join(g[2] for g in sorted((g for g in groups if g[0] < m.start() < g[1]), key=lambda g: g[0]))
        cls, action = _target(args[1] if len(args) > 1 else "", "")
        if not cls and len(args) > 1:
            pair = re.search(r"\\?([\w\\]+)::class\s*\.\s*['\"]:(\w+)['\"]|['\"]([\w\\]+):(\w+)['\"]", args[1])
            cls, action = (pair.group(1) or pair.group(3), pair.group(2) or pair.group(4)) if pair else ("", "")
        out += [Route(v, join_path(prefix, lits[0]), cls, action, line_of(text, m.start())) for v in verbs]
    return out


SYMFONY_DECL = re.compile(r"\s*(?:#\[[^\]]*\]\s*)*(?:(?:final|abstract|public|protected|private|static|readonly)\s+)*(class|function)\s+(\w+)")


def _symfony_spec(args: str) -> tuple[str, list[str]]:
    named = re.search(r"\bpath\s*[:=]\s*(['\"])([^'\"]*)\1", args)
    lits = literals(re.sub(r"\b\w+\s*[:=]\s*(\[[^\]]*\]|\{[^}]*\}|['\"][^'\"]*['\"])", "", args))
    path = named.group(2) if named else (lits[0] if lits else "")
    meth = re.search(r"\bmethods\s*[:=]\s*(\[[^\]]*\]|\{[^}]*\}|['\"][^'\"]*['\"])", args)
    verbs = [v.upper() for v in literals(meth.group(1))] if meth else ["ANY"]
    return path, verbs


def symfony(text: str) -> list[Route]:
    code = blank(text)
    found: list[tuple[int, int, str, list[str]]] = []   # (where the declaration starts, its line, path, verbs)
    for m in re.finditer(r"#\[\s*(?:\\?[\w\\]*\\)?Route\s*\(", code):
        close = match_close(code, m.end() - 1, "(", ")")
        after = code.find("]", close)
        found.append((after + 1 if after >= 0 else close + 1, 0, *_symfony_spec(code[m.end():close])))
    for doc in re.finditer(r"/\*\*.*?\*/", text, re.S):
        for m in re.finditer(r"@Route\s*\(", doc.group(0)):
            start = doc.start() + m.end() - 1
            close = match_close(text, start, "(", ")")
            found.append((doc.end(), 0, *_symfony_spec(text[start + 1:close])))
    bare = blank(text, True)
    classes: list[tuple[int, int, str]] = []
    methods = []
    for at, _, path, verbs in sorted(found):
        d = SYMFONY_DECL.match(code, at)
        if not d:
            continue
        if d.group(1) == "class":
            brace = bare.find("{", d.end())
            classes.append((brace, match_close(bare, brace) if brace >= 0 else brace, path))
        else:
            methods.append((d.start(2), d.group(2), path, verbs))
    out = []
    for pos, name, path, verbs in methods:
        prefix = next((c[2] for c in classes if c[0] < pos < c[1]), "")
        out += [Route(v, join_path(prefix, path), "", name, line_of(text, pos)) for v in verbs]
    return out
