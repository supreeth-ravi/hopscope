"""Rails config/routes.rb without running it: resources/resource (only/except, param, nested, member/collection blocks
and on:), namespace/scope (path and module), verb routes (get 'p' => 'c#a', get 'p', to: 'c#a', match … via:), root,
and the sign-in/sign-out/sign-up routes of devise_for, concerns (`concern :x do … end` replayed where `concerns: :x` or
`concerns :x` uses it), shallow nesting (`shallow: true`, `shallow do`) and brace member/collection blocks. Each route
names its controller and action.

Choices: `resources` emits the framework's full set (new and edit included) unless only:/except: say otherwise; update
is both PATCH and PUT, as `rails routes` lists it."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from langs.rubytext import blank, blocks, plural, singular, statements

VERBS = ("get", "post", "put", "patch", "delete", "options", "head")
PLURAL_ACTIONS = (("index", "GET", ""), ("create", "POST", ""), ("new", "GET", "/new"), ("edit", "GET", "/{m}/edit"),
                  ("show", "GET", "/{m}"), ("update", "PATCH", "/{m}"), ("update", "PUT", "/{m}"), ("destroy", "DELETE", "/{m}"))
SINGULAR_ACTIONS = (("create", "POST", ""), ("new", "GET", "/new"), ("edit", "GET", "/edit"), ("show", "GET", ""),
                    ("update", "PATCH", ""), ("update", "PUT", ""), ("destroy", "DELETE", ""))
DO_AT_END = re.compile(r"\bdo\s*(?:\|[^|]*\|)?\s*$")
HEAD = re.compile(r"\s*(\w+)\b")


@dataclass
class Resource:
    base: str        # /api/teas
    param: str       # slug
    single: bool
    name: str        # teas
    controller: str  # api/teas
    member_base: str = ""  # where member routes live: base, or the scope's root path for shallow nesting


@dataclass
class Scope:
    path: str = ""
    module: str = ""
    controller: str = ""
    res: Resource | None = None
    mode: str = ""   # "", "body" (inside a resources block), "member", "collection", "concern" (a body not drawn here)
    root: str = ""   # the namespace/scope path, without resource nesting (shallow member routes live there)
    shallow: bool = False


@dataclass
class Route:
    verb: str
    path: str
    controller: str  # api/teas ("" when unknown)
    action: str
    line: int


@dataclass
class Args:
    pos: list = field(default_factory=list)
    kw: dict = field(default_factory=dict)
    to: str = ""     # 'blends#show' from get 'p' => 'blends#show'


def split(args: str) -> list[str]:
    """Top-level comma split that respects quotes and ( [ { (Ruby's => is not a bracket)."""
    out: list[str] = []
    cur: list[str] = []
    depth, quote = 0, ""
    for ch in args:
        if quote:
            quote = "" if ch == quote and (not cur or cur[-1] != "\\") else quote
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur))
    return out


def value(v: str) -> Any:
    """A Ruby literal as Python: :sym / 'str' -> str, [..] / %i[..] -> list, { k: v } -> dict; other code -> ''."""
    v = v.strip()
    m = re.fullmatch(r"%[iIwW][\[(](.*)[\])]", v, re.S)
    if m:
        return m.group(1).split()
    if v.startswith("[") and v.endswith("]"):
        return [value(p) for p in split(v[1:-1]) if p.strip()]
    if v.startswith("{") and v.endswith("}"):
        return dict(p for p in (_pair(x) for x in split(v[1:-1])) if p is not None)
    if v in ("true", "false"):
        return v == "true"
    m = re.fullmatch(r":(\w+[?!]?)|'([^']*)'|\"([^\"]*)\"", v)
    return next((g for g in m.groups() if g is not None), "") if m else ""


def _pair(part: str) -> tuple[str, Any] | None:
    m = re.match(r"\s*(?:(\w+):(?!:)|:(\w+)\s*=>|['\"](\w+)['\"]\s*=>)\s*(.*)$", part, re.S)
    return (m.group(1) or m.group(2) or m.group(3), value(m.group(4))) if m else None


def parse_args(text: str) -> Args:
    text = text.strip()
    text = re.sub(DO_AT_END, "", text).strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    a = Args()
    for part in split(text):
        rocket = re.match(r"\s*(['\"])([^'\"]*)\1\s*=>\s*(['\"])([^'\"]*)\3\s*$", part)
        pair = _pair(part)
        if rocket:
            a.pos.append(rocket.group(2))
            a.to = rocket.group(4)
        elif pair:
            a.kw[pair[0]] = pair[1]
        else:
            a.pos.append(value(part))
    return a


def _join(*parts: str) -> str:
    segs = [p.strip("/") for p in parts if p and p.strip("/")]
    return "/" + "/".join(segs) if segs else "/"


def _mod(scope: Scope, name: str) -> str:
    return "/".join(p for p in (scope.module, name) if p)


def _nested(scope: Scope) -> str:
    """The path a resource block gives what is nested inside it."""
    r = scope.res
    if r is None:
        return scope.path
    return r.base if r.single else _join(r.member_base or r.base, f":{singular(r.name)}_{r.param}")


def _open(scope: Scope, name: str, a: Args) -> Scope:
    """The scope a `do` block opens."""
    first = a.pos[0] if a.pos and isinstance(a.pos[0], str) else ""
    if name == "namespace":
        path = _join(scope.path, str(a.kw.get("path", first)))
        return replace(scope, path=path, root=path, module=_mod(scope, str(a.kw.get("module", first))), res=None, mode="")
    if name == "scope":
        path = str(a.kw.get("path", first))
        joined = _join(scope.path, path)
        return replace(scope, path=joined, root=joined if scope.res is None else scope.root, module=_mod(scope, str(a.kw.get("module", ""))),
                       controller=str(a.kw.get("controller", scope.controller)), shallow=scope.shallow or a.kw.get("shallow") is True)
    if name == "shallow":
        return replace(scope, shallow=True)
    if name == "concern":
        return replace(scope, mode="concern")
    if name == "controller":
        return replace(scope, controller=first)
    if name in ("member", "collection") and scope.res is not None:
        r = scope.res
        path = r.base if name == "collection" or r.single else _join(r.member_base or r.base, f":{r.param}")
        return replace(scope, path=path, mode=name)
    return scope


def _resources(scope: Scope, name: str, a: Args, line: int) -> tuple[list[Route], Scope]:
    single = name == "resource"
    only, exc = a.kw.get("only"), a.kw.get("except")
    only = [only] if isinstance(only, str) else only
    exc = [exc] if isinstance(exc, str) else (exc or [])
    out: list[Route] = []
    nested = replace(scope, path=_nested(scope), mode="") if scope.mode == "body" else scope
    own_shallow = scope.shallow and scope.mode == "body"  # nested under a shallow resource: member routes drop the parent
    inner = scope
    for res in [p for p in a.pos if isinstance(p, str) and p]:
        base = _join(nested.path, str(a.kw.get("path", res)))
        member_base = _join(scope.root, str(a.kw.get("path", res))) if own_shallow else base
        param = str(a.kw.get("param", "id"))
        ctrl = _mod(nested, str(a.kw.get("controller", plural(res) if single else res)))
        for action, verb, tail in SINGULAR_ACTIONS if single else PLURAL_ACTIONS:
            if (only is not None and action not in only) or action in exc:
                continue
            at = member_base if "{m}" in tail else base
            out.append(Route(verb, at + tail.replace("{m}", f":{param}"), ctrl, action, line))
        inner = replace(nested, res=Resource(base, param, single, res, ctrl, member_base), mode="body",
                        shallow=scope.shallow or a.kw.get("shallow") is True)
    return out, inner


def _target(scope: Scope, a: Args, path: str) -> tuple[str, str]:
    to = a.to or (a.kw.get("to") if isinstance(a.kw.get("to"), str) else "")
    if to and "#" in to:
        c, act = to.split("#", 1)
        return _mod(scope, c), act
    if "controller" in a.kw or "action" in a.kw:
        return _mod(scope, str(a.kw.get("controller", scope.controller))), str(a.kw.get("action", ""))
    name = path.strip("/").split("/")[-1].lstrip(":") if path else ""
    if scope.res is not None:
        return scope.res.controller, name
    if scope.controller:
        return _mod(scope, scope.controller), name
    segs = [s for s in path.strip("/").split("/") if s and not s.startswith(":")]
    return (_mod(scope, segs[0]), segs[-1]) if len(segs) >= 2 else ("", name)


def _verb_route(scope: Scope, name: str, a: Args, line: int) -> list[Route]:
    path = a.pos[0] if a.pos and isinstance(a.pos[0], str) else ""
    if name == "root":
        ctrl, act = _target(scope, Args(to=a.to or str(a.kw.get("to", path))), "")
        return [Route("GET", _join(scope.path), ctrl, act, line)]
    if scope.mode == "body" and scope.res is not None:
        on = a.kw.get("on", "")
        r = scope.res
        if on == "collection" or (on == "" and r.single):
            prefix = r.base
        elif on == "member":
            prefix = _join(r.member_base or r.base, f":{r.param}")
        else:
            prefix = _nested(scope)
    else:
        prefix = scope.path
    via = a.kw.get("via", "get")
    verbs = [name.upper()] if name != "match" else [str(v).upper() for v in (via if isinstance(via, list) else [via])]
    ctrl, act = _target(scope, a, path)
    return [Route(v, _join(prefix, path), ctrl, act, line) for v in verbs if v.lower() in VERBS or v == "ALL"]


def _devise(scope: Scope, a: Args, line: int) -> list[Route]:
    out: list[Route] = []
    names = a.pos[0] if a.pos and isinstance(a.pos[0], str) else "users"
    base = _join(scope.path, str(a.kw.get("path", names)))
    ctrls: dict[str, Any] = a.kw["controllers"] if isinstance(a.kw.get("controllers"), dict) else {}
    pn: dict[str, Any] = a.kw["path_names"] if isinstance(a.kw.get("path_names"), dict) else {}
    skip = a.kw.get("skip") or []
    skip = [skip] if isinstance(skip, str) else skip

    def ctrl(kind: str) -> str:
        return _mod(scope, str(ctrls.get(kind, ""))) if ctrls.get(kind) else f"devise/{kind}"
    if "sessions" not in skip:
        out.append(Route("POST", _join(base, str(pn.get("sign_in", "sign_in"))), ctrl("sessions"), "create", line))
        out.append(Route("DELETE", _join(base, str(pn.get("sign_out", "sign_out"))), ctrl("sessions"), "destroy", line))
    if "registrations" not in skip:
        out.append(Route("POST", base, ctrl("registrations"), "create", line))
    return out


@dataclass
class Walk:
    code: str
    bare: str
    ends: dict[int, int]
    concerns: dict[str, tuple[int, int]] = field(default_factory=dict)
    out: list[Route] = field(default_factory=list)


OPENERS = ("namespace", "scope", "controller", "member", "collection", "shallow", "concern")
NAMED_ARGS = VERBS + ("match", "root", "resources", "resource", "namespace", "scope", "devise_for", "controller", "concern", "concerns")


def _replay(w: Walk, names: Any, scope: Scope, depth: int) -> None:
    """Draw the routes of the named concerns inside `scope` (a resource body)."""
    for n in names if isinstance(names, list) else [names]:
        if isinstance(n, str) and n in w.concerns and depth < 8:
            lo, hi = w.concerns[n]
            _walk(w, lo, hi, replace(scope, mode="body") if scope.res is not None else scope, depth + 1)


def _walk(w: Walk, lo: int, hi: int, scope0: Scope, depth: int = 0) -> None:
    """The statements between offsets lo and hi of w.code, drawn in scope0."""
    stack: list[tuple[int, Scope]] = [(hi, scope0)]
    for s, e in ((lo + s, lo + e) for s, e in statements(w.code[lo:hi])):
        while len(stack) > 1 and stack[-1][0] <= s:
            stack.pop()
        scope = stack[-1][1]
        head = HEAD.match(w.code, s)
        if not head or scope.mode == "concern":
            continue
        name, rest = head.group(1), w.code[head.end():e]
        line = w.code.count("\n", 0, s) + 1
        a = parse_args(rest) if name in NAMED_ARGS else Args()
        inner = scope
        if name in VERBS or name in ("match", "root"):
            w.out += _verb_route(scope, name, a, line)
        elif name in ("resources", "resource"):
            found, inner = _resources(scope, name, a, line)
            w.out += found
            if a.kw.get("concerns"):
                _replay(w, a.kw["concerns"], inner, depth)
        elif name == "devise_for":
            w.out += _devise(scope, a, line)
        elif name == "concerns":
            _replay(w, a.pos[0] if len(a.pos) == 1 else a.pos, scope, depth)
        elif name in OPENERS:
            inner = _open(scope, name, a)
        do = DO_AT_END.search(w.bare[s:e])
        if do and s + do.start() in w.ends:
            end = w.ends[s + do.start()]
            if name == "concern" and a.pos and isinstance(a.pos[0], str):
                w.concerns[a.pos[0]] = (s + do.end(), end)
            stack.append((end, inner))
            continue
        brace = re.match(r"\s*\{", w.code[head.end():e]) if name in ("member", "collection", "shallow") else None
        if brace:  # member { post :steep } / collection { … } on one or several lines
            close = w.code.rfind("}", head.end(), e)
            if close > head.end() + brace.end():
                _walk(w, head.end() + brace.end(), close, inner, depth + 1)


def parse(text: str) -> list[Route]:
    """Every route a routes.rb file draws."""
    code = blank(text)
    bare = blank(text, True)
    w = Walk(code, bare, {b.pos: b.end for b in blocks(bare) if b.kw == "do"})
    _walk(w, 0, len(code), Scope())
    return w.out
