"""Sinatra and Grape routes: `get '/x' do … end` blocks (and post/put/patch/delete/head/options), nested in Sinatra
`namespace '/x' do` or Grape `resource/resources/namespace/group/segment :x do` and `route_param :id do` blocks, with
Grape's `prefix` and path `version` in front."""
from __future__ import annotations

import re

from langs.common import line_of
from langs.rails import DO_AT_END, parse_args
from langs.rubytext import blank, blocks, statements

VERB = re.compile(r"\s*(get|post|put|patch|delete|head|options)\b(?!\s*[:=.(]\s*[a-z_])")
FRAME = re.compile(r"\s*(namespace|resource|resources|group|segment|route_param|scope)\b")
DESC = re.compile(r"\s*desc\s+['\"]([^'\"]*)['\"]")


def is_dsl(text: str) -> bool:
    return bool(re.search(r"\bSinatra\b|require\s+['\"]sinatra|\bGrape::API\b", text))


def _piece(name: str, args: str) -> str:
    a = parse_args(args)
    first = next((p for p in a.pos if isinstance(p, str) and p), "")
    return f":{first}" if name == "route_param" and first else first


def _grape_prefix(code: str) -> str:
    parts = []
    m = re.search(r"^\s*prefix\s+\(?\s*[:'\"]?([\w/.-]+)", code, re.M)
    if m:
        parts.append(m.group(1))
    v = re.search(r"^\s*version\s+\(?\s*['\"]([\w.]+)['\"]([^\n]*)", code, re.M)
    if v and ("using" not in v.group(2) or re.search(r"using:\s*:path|:using\s*=>\s*:path", v.group(2))):
        parts.append(v.group(1))
    return "/".join(p.strip("/") for p in parts)


def _join(*parts: str) -> str:
    segs = [p.strip("/") for p in parts if p and p.strip("/")]
    return "/" + "/".join(segs) if segs else "/"


def routes(text: str) -> list[dict]:
    code, bare = blank(text), blank(text, True)
    ends = {b.pos: b.end for b in blocks(bare) if b.kw == "do"}
    grape = "Grape::API" in text
    root = _grape_prefix(code) if grape else ""
    stack: list[tuple[int, str]] = []
    out: list[dict] = []
    desc = ""
    for s, e in statements(code):
        while stack and stack[-1][0] <= s:
            stack.pop()
        prefix = stack[-1][1] if stack else root
        do = DO_AT_END.search(bare[s:e])
        d = DESC.match(code, s)
        if d:
            desc = d.group(1)
        verb, frame = VERB.match(code, s), FRAME.match(code, s)
        if verb and do:
            path = _piece(verb.group(1), code[verb.end():e])
            out.append({"m": verb.group(1).upper(), "p": _join(prefix, path), "t": "", "d": desc[:140], "l": line_of(text, verb.start(1))})
            desc = ""
        if do and s + do.start() in ends:
            inner = _join(prefix, _piece(frame.group(1), code[frame.end():e])) if frame else prefix
            stack.append((ends[s + do.start()], inner))
    return out
