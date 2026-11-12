"""JavaScript and TypeScript: Express-style routes (app/router.get(...)), NestJS controllers, relative imports."""
from __future__ import annotations

import re
from pathlib import Path

from langs.common import humanize, line_of, shared_externals

NAME = "js"
EXTENSIONS = (".js", ".jsx", ".ts", ".tsx", ".mjs")
SERVICE_MARKERS = ("package.json",)

JS_ROUTE = re.compile(r"\b(?:app|router|server|api|\w+Router)\.(get|post|put|patch|delete|all)\(\s*['\"`]([^'\"`]+)['\"`]")
NEST_CTRL = re.compile(r"@Controller\(\s*['\"`]?([^'\"`)]*)['\"`]?\s*\)")
NEST_ROUTE = re.compile(r"@(Get|Post|Put|Patch|Delete)\(\s*['\"`]?([^'\"`)]*)['\"`]?\s*\)\s*\n\s*(?:async\s+)?(\w+)")
JS_IMPORT = re.compile(r"(?:import[^'\"]*from\s*|require\(\s*|import\(\s*)['\"](\.{1,2}/[^'\"]+)['\"]")


def routes(path: Path, text: str, ctx=None) -> list[dict]:
    out = []
    for m in JS_ROUTE.finditer(text):
        out.append({"m": m.group(1).upper().replace("ALL", "ANY"), "p": m.group(2), "t": "", "d": "", "l": line_of(text, m.start())})
    ctrl = NEST_CTRL.search(text)
    base = "/" + ctrl.group(1).strip("/") if ctrl and ctrl.group(1) else ""
    for m in NEST_ROUTE.finditer(text):
        sub = ("/" + m.group(2).strip("/")) if m.group(2) else ""
        out.append({"m": m.group(1).upper(), "p": (base + sub) or "/", "t": humanize(m.group(3)), "d": "", "l": line_of(text, m.start())})
    return out


def _resolve(path: Path, spec: str, index: dict[str, int]) -> int | None:
    base = (path.parent / spec).resolve()
    for cand in (base, *(base.with_suffix(e) for e in (".ts", ".tsx", ".js", ".jsx", ".mjs")), *(base / f"index{e}" for e in (".ts", ".tsx", ".js"))):
        i = index.get(str(cand))
        if i is not None:
            return i
    return None


def imports(path: Path, text: str, ctx) -> list[int]:
    out = []
    for m in JS_IMPORT.finditer(text):
        i = _resolve(path, m.group(1), ctx.index)
        if i is not None:
            out.append(i)
    return out


USE = re.compile(r"\b\w+\.use\(\s*['\"`]([^'\"`]+)['\"`]\s*,\s*(?:require\(\s*['\"](\.{1,2}/[^'\"]+)['\"]\s*\)|(\w+))")
NAMED_IMPORT = re.compile(r"\bimport\s+(?:(\w+)\s*,?\s*)?(?:\{([^}]*)\})?\s*from\s*['\"](\.{1,2}/[^'\"]+)['\"]|"
                          r"\b(?:const|let|var)\s+(\w+)\s*=\s*require\(\s*['\"](\.{1,2}/[^'\"]+)['\"]\s*\)")


def mounts(path: Path, text: str, ctx) -> list[tuple[int, str]]:
    """Express routers mounted under a prefix: app.use('/api/v1', ordersRouter) -> [(router file, '/api/v1')]."""
    if ".use(" not in text:
        return []
    names: dict[str, str] = {}
    for m in NAMED_IMPORT.finditer(text):
        if m.group(4):
            names[m.group(4)] = m.group(5)
            continue
        if m.group(1):
            names[m.group(1)] = m.group(3)
        for item in (m.group(2) or "").split(","):
            if item.strip():
                names[item.strip().split(" as ")[-1].strip()] = m.group(3)
    out = []
    for m in USE.finditer(text):
        spec = m.group(2) or names.get(m.group(3) or "")
        i = _resolve(path, spec, ctx.index) if spec and m.group(1).startswith("/") else None
        if i is not None:
            out.append((i, m.group(1)))
    return out


def externals(text: str, ctx=None) -> set[str]:
    return shared_externals(text)


def client_calls(path: Path, text: str, ctx=None) -> list[tuple[int, str, str]]:
    """HTTP calls this file makes to paths (frontends): see langs/jsclient.py."""
    from langs import jsclient
    return jsclient.client_calls(path, text, ctx)
