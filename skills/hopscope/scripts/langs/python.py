"""Python: FastAPI, Flask and Starlette-style routes, imports resolved to files, router mounts with prefixes."""
from __future__ import annotations

import ast
import re
from functools import lru_cache
from pathlib import Path

from langs.common import humanize, shared_externals

NAME = "python"
EXTENSIONS = (".py",)
SERVICE_MARKERS = ("Dockerfile", "pyproject.toml", "requirements.txt", "setup.py")

PY_ROUTE = re.compile(r"^(app|router|api|bp|blueprint|\w+_router|\w+_app)$")
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "websocket", "api_route", "route")


@lru_cache(maxsize=8)
def parse(text: str) -> ast.AST | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


def routes(path: Path, text: str, ctx=None) -> list[dict]:
    tree = parse(text)
    if tree is None:
        return []
    out = []
    prefixes = {}
    for node in ast.walk(tree):  # router = APIRouter(prefix="/x")
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            for kw in node.value.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant) and isinstance(node.targets[0], ast.Name):
                    prefixes[node.targets[0].id] = str(kw.value.value)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and isinstance(dec.func.value, ast.Name)):
                continue
            obj, meth = dec.func.value.id, dec.func.attr.lower()
            if meth not in HTTP_METHODS or not (PY_ROUTE.match(obj) or obj in prefixes) or not dec.args or not isinstance(dec.args[0], ast.Constant):
                continue
            rpath = prefixes.get(obj, "") + str(dec.args[0].value)
            methods = [meth.upper()]
            if meth in ("route", "api_route"):
                methods = ["GET"]
                for kw in dec.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        methods = [str(e.value).upper() for e in kw.value.elts if isinstance(e, ast.Constant)] or ["GET"]
            if meth == "websocket":
                methods = ["WS"]
            doc = (ast.get_docstring(node) or "").strip().split("\n")[0][:140]
            for m in methods:
                out.append({"m": m, "p": rpath or "/", "t": humanize(node.name), "d": doc, "l": node.lineno})
    return out


def _imports_from(tree: ast.AST, path: Path, root: Path, index: dict[str, int]) -> list[int]:
    targets = []
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = path.parent
                for _ in range(node.level - 1):
                    parent = parent.parent
                rel = parent.joinpath(*base.split(".")) if base else parent
                mods = [rel] + [rel / a.name for a in node.names]
            else:
                mods = [root.joinpath(*base.split("."))] + [root.joinpath(*base.split("."), a.name) for a in node.names]
        elif isinstance(node, ast.Import):
            mods = [root.joinpath(*a.name.split(".")) for a in node.names]
        for m in mods:
            for cand in (m.with_suffix(".py"), m / "__init__.py"):
                i = index.get(str(cand))
                if i is not None:
                    targets.append(i)
                    break
    return sorted(set(targets))


def imports(path: Path, text: str, ctx) -> list[int]:
    """Files this one imports: absolute imports resolve against the service root, then the repo root (src layouts)."""
    tree = parse(text)
    if tree is None:
        return []
    links = _imports_from(tree, path.resolve(), ctx.root.resolve(), ctx.index)
    if not links and ctx.root != ctx.repo:
        links = _imports_from(tree, path.resolve(), ctx.repo.resolve(), ctx.index)
    return links


def externals(text: str, ctx=None) -> set[str]:
    return shared_externals(text)


def _names(tree: ast.AST, path: Path, root: Path, index: dict[str, int]) -> dict[str, int]:
    """Imported names that resolve to files in this codebase: {local name: file index}."""
    names = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        base = node.module or ""
        if node.level:
            parent = path.parent
            for _ in range(node.level - 1):
                parent = parent.parent
            pkg = parent.joinpath(*base.split(".")) if base else parent
        else:
            pkg = root.joinpath(*base.split("."))
        for a in node.names:
            for cand in (pkg / f"{a.name}.py", pkg / a.name / "__init__.py", pkg.with_suffix(".py"), pkg / "__init__.py"):
                i = index.get(str(cand))
                if i is not None:
                    names[a.asname or a.name] = i
                    break
    return names


def mounts(path: Path, text: str, ctx) -> list[tuple[int, str]]:
    """include_router(x.router, prefix=...) / register_blueprint(bp, url_prefix=...) -> [(mounted file, prefix)]."""
    tree = parse(text)
    if tree is None:
        return []
    names = _names(tree, path.resolve(), ctx.root.resolve(), ctx.index)
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("include_router", "register_blueprint") and node.args):
            continue
        arg = node.args[0]
        head = arg.value.id if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name) else arg.id if isinstance(arg, ast.Name) else None
        prefix = next((str(k.value.value) for k in node.keywords if k.arg in ("prefix", "url_prefix") and isinstance(k.value, ast.Constant)), "")
        if head in names:
            out.append((names[head], prefix))
    return out
