"""Map one or more code repositories: services, files, API routes, imports and the external
systems each file talks to. Writes code.json (the console's code map).

    python code_scan.py --repo ~/src/backend --repo ~/src/web --out data/code.json

If a repo already has a floorplan map (floorplan/graph.json), its human-readable module and file
names are used; otherwise the built-in scanner reads Python, JavaScript/TypeScript and Go.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", ".next", "site-packages", ".tox", ".mypy_cache",
             "coverage", "vendor", ".terraform", "migrations", "alembic", "tests", "test", "__tests__", "floorplan"}
CODE_EXT = {".py": "python", ".js": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".mjs": "js", ".go": "go"}
SERVICE_MARKERS = ("Dockerfile", "pyproject.toml", "requirements.txt", "package.json", "go.mod", "setup.py")
MAX_FILE_BYTES = 400_000

# External systems, detected from imports and SDK calls. Keys are what the console draws.
EXTERNAL = {
    "postgres": r"^\s*(from|import)\s+(sqlalchemy|sqlmodel|asyncpg|psycopg2?|alembic|tortoise|peewee|django\.db)\b|from\s+['\"](pg|postgres|@prisma/client|typeorm|sequelize|knex|drizzle-orm)['\"]|\"github\.com/(lib/pq|jackc/pgx)",
    "mysql": r"^\s*(from|import)\s+(pymysql|MySQLdb|aiomysql)\b|from\s+['\"]mysql2?['\"]",
    "mongo": r"^\s*(from|import)\s+(pymongo|motor|beanie|mongoengine)\b|from\s+['\"]mongoose|mongodb['\"]",
    "cache": r"^\s*(from|import)\s+(redis|aioredis|valkey)\b|from\s+['\"](ioredis|redis)['\"]",
    "llm": r"^\s*(from|import)\s+(openai|litellm|anthropic)\b|from\s+['\"](openai|@anthropic-ai/sdk)['\"]",
    "cognito": r"cognito-idp|CognitoIdentityProvider|cognito_idp",
    "sqs": r"['\"]sqs['\"]|client-sqs|send_message\(|SendMessageCommand",
    "ses": r"['\"]sesv?2?['\"]|client-ses|send_email\(|SendEmailCommand",
    "s3": r"['\"]s3['\"]|client-s3|put_object\(|upload_fileobj\(|PutObjectCommand",
    "search": r"opensearch|elasticsearch",
    "chatapps": r"slack_sdk|slack_bolt|@slack/|telegram",
    "tracing": r"opentelemetry|langfuse|@sentry|sentry_sdk|datadog|ddtrace",
}
EXT_RX = {k: re.compile(v, re.M | re.I) for k, v in EXTERNAL.items()}

PY_ROUTE = re.compile(r"^(app|router|api|bp|blueprint|\w+_router|\w+_app)$")
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "websocket", "api_route", "route")
JS_ROUTE = re.compile(r"\b(?:app|router|server|api|\w+Router)\.(get|post|put|patch|delete|all)\(\s*['\"`]([^'\"`]+)['\"`]")
NEST_CTRL = re.compile(r"@Controller\(\s*['\"`]?([^'\"`)]*)['\"`]?\s*\)")
NEST_ROUTE = re.compile(r"@(Get|Post|Put|Patch|Delete)\(\s*['\"`]?([^'\"`)]*)['\"`]?\s*\)\s*\n\s*(?:async\s+)?(\w+)")
GO_ROUTE = re.compile(r"\.(GET|POST|PUT|PATCH|DELETE|HandleFunc|Handle)\(\s*\"([^\"]+)\"")
JS_IMPORT = re.compile(r"(?:import[^'\"]*from\s*|require\(\s*|import\(\s*)['\"](\.{1,2}/[^'\"]+)['\"]")


@dataclass
class CodeMap:
    files: list[dict] = field(default_factory=list)
    routes: list[dict] = field(default_factory=list)
    links: list[list] = field(default_factory=list)
    modules: list[dict] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    flows: list[dict] = field(default_factory=list)
    module_links: list[dict] = field(default_factory=list)


def humanize(name: str) -> str:
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", re.sub(r"[_\-.]+", " ", name)).strip().split()
    return " ".join(words).capitalize() if words else name


def walk(repo: Path):
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            p = Path(dirpath) / f
            if p.suffix in CODE_EXT and p.stat().st_size < MAX_FILE_BYTES and not re.search(r"(\.test|\.spec|_test)\.\w+$|\.d\.ts$|\.min\.js$", f):
                yield p


def service_roots(repo: Path, files: list[Path]) -> list[Path]:
    """Folders that look like deployable services: nearest ancestor with a Dockerfile or package manifest."""
    roots = set()
    for f in files:
        d = f.parent
        while True:
            if any((d / m).exists() for m in SERVICE_MARKERS):
                roots.add(d)
                break
            if d == repo or d == d.parent:
                roots.add(repo)
                break
            d = d.parent
    # Drop a root that only contains other roots (e.g. a monorepo with a top-level package.json).
    return sorted(roots, key=lambda p: (len(p.parts), str(p)))


def owning_root(path: Path, roots: list[Path]) -> Path:
    best = None
    for r in roots:
        if path == r or r in path.parents:
            if best is None or len(r.parts) > len(best.parts):
                best = r
    return best or roots[0]


def py_routes(tree: ast.AST, src: str) -> list[dict]:
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
            path = prefixes.get(obj, "") + str(dec.args[0].value)
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
                out.append({"m": m, "p": path or "/", "t": humanize(node.name), "d": doc, "l": node.lineno})
    return out


def js_routes(src: str) -> list[dict]:
    out = []
    def line_of(i: int) -> int:
        return src.count("\n", 0, i) + 1

    for m in JS_ROUTE.finditer(src):
        out.append({"m": m.group(1).upper().replace("ALL", "ANY"), "p": m.group(2), "t": "", "d": "", "l": line_of(m.start())})
    ctrl = NEST_CTRL.search(src)
    base = "/" + ctrl.group(1).strip("/") if ctrl and ctrl.group(1) else ""
    for m in NEST_ROUTE.finditer(src):
        sub = ("/" + m.group(2).strip("/")) if m.group(2) else ""
        out.append({"m": m.group(1).upper(), "p": (base + sub) or "/", "t": humanize(m.group(3)), "d": "", "l": line_of(m.start())})
    return out


def go_routes(src: str) -> list[dict]:
    return [{"m": ("ANY" if m.group(1).startswith("Handle") else m.group(1)), "p": m.group(2), "t": "", "d": "", "l": src.count("\n", 0, m.start()) + 1}
            for m in GO_ROUTE.finditer(src) if m.group(2).startswith("/")]


def externals(src: str) -> list[str]:
    found = [k for k, rx in EXT_RX.items() if rx.search(src)]
    return sorted(found)


def py_imports(tree: ast.AST, path: Path, root: Path, index: dict[str, int]) -> list[int]:
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
    # Also resolve absolute imports against every ancestor up to the service root (src layouts).
    return sorted(set(targets))


def py_names(tree: ast.AST, path: Path, root: Path, index: dict[str, int]) -> dict[str, int]:
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


def py_mounts(tree: ast.AST, names: dict[str, int]) -> list[tuple[int, str]]:
    """include_router(x.router, prefix=...) / register_blueprint(bp, url_prefix=...) -> [(mounted file, prefix)]."""
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


def full_prefix(i: int, mounted_by: dict[int, list[tuple[int, str]]], seen: frozenset = frozenset()) -> str:
    """Prefix a file's routes inherit from the files that mount it (first mount wins)."""
    if i not in mounted_by or i in seen:
        return ""
    parent, prefix = mounted_by[i][0]
    return full_prefix(parent, mounted_by, seen | {i}) + prefix


def js_imports(src: str, path: Path, index: dict[str, int]) -> list[int]:
    out = []
    for m in JS_IMPORT.finditer(src):
        base = (path.parent / m.group(1)).resolve()
        for cand in (base, *(base.with_suffix(e) for e in (".ts", ".tsx", ".js", ".jsx", ".mjs")), *(base / f"index{e}" for e in (".ts", ".tsx", ".js"))):
            i = index.get(str(cand))
            if i is not None:
                out.append(i)
                break
    return out


def scan(repos: list[Path]) -> CodeMap:
    cm = CodeMap()
    common = Path(os.path.commonpath([str(r) for r in repos])) if len(repos) > 1 else repos[0]
    all_files: list[tuple[Path, Path]] = []
    roots: list[Path] = []
    for repo in repos:
        files = list(walk(repo))
        rs = service_roots(repo, files)
        roots += rs
        all_files += [(f, repo) for f in files]
    def rel(p: Path) -> str:
        return str(p.relative_to(common))

    index = {str(f.resolve()): i for i, (f, _) in enumerate(all_files)}
    root_ids = {str(r): (rel(r) if r != common else ".") for r in roots}
    cm.services = sorted(set(root_ids.values()))
    module_stats: dict[str, dict] = {}
    mounted_by: dict[int, list[tuple[int, str]]] = {}
    for i, (f, repo) in enumerate(all_files):
        src = f.read_text(errors="ignore")
        root = owning_root(f, roots)
        sid = root_ids[str(root)]
        sub = f.relative_to(root).parts
        mid = f"{sid}::{sub[0] if len(sub) > 1 else '(root)'}"
        lang = CODE_EXT[f.suffix]
        routes, links = [], []
        if lang == "python":
            try:
                tree = ast.parse(src)
                routes = py_routes(tree, src)
                links = py_imports(tree, f.resolve(), root.resolve(), index)
                if not links and root != repo:
                    links = py_imports(tree, f.resolve(), repo.resolve(), index)
                names = py_names(tree, f.resolve(), root.resolve(), index)
                for child, prefix in py_mounts(tree, names):
                    mounted_by.setdefault(child, []).append((i, prefix))
            except (SyntaxError, ValueError):
                pass
        elif lang == "js":
            routes, links = js_routes(src), js_imports(src, f, index)
        else:
            routes = go_routes(src)
        cm.files.append({"p": rel(f), "m": mid, "t": humanize(f.stem), "d": "", "x": externals(src), "loc": src.count("\n") + 1})
        for r in routes:
            cm.routes.append({**r, "f": i})
        cm.links += [[i, j, 0, ""] for j in links if j != i]
        st = module_stats.setdefault(mid, {"files": 0, "loc": 0, "service": sid})
        st["files"] += 1
        st["loc"] += src.count("\n") + 1
    for r in cm.routes:  # apply router prefixes now that every mount is known
        pre = full_prefix(r["f"], mounted_by)
        if pre:
            r["p"] = (pre.rstrip("/") + "/" + r["p"].lstrip("/")).rstrip("/") or "/"
    for sid in cm.services:
        mods = {k: v for k, v in module_stats.items() if v["service"] == sid}
        cm.modules.append({"id": sid, "parent": None, "label": humanize(Path(sid).name), "kind": "service", "summary": "",
                           "paths": [sid + "/**"], "files": sum(v["files"] for v in mods.values()), "loc": sum(v["loc"] for v in mods.values())})
        for mid, v in mods.items():
            name = mid.split("::")[1]
            cm.modules.append({"id": mid, "parent": sid, "label": humanize(name) if name != "(root)" else "Entry points", "kind": "module", "summary": "",
                               "paths": [f"{sid}/{name}/**"], "files": v["files"], "loc": v["loc"]})
    return cm


def from_floorplan(graph_path: Path, repo: Path) -> CodeMap:
    """Use an existing floorplan map: better names and descriptions than the raw scan."""
    g = json.loads(graph_path.read_text())
    files = g["files"]
    index = {f["p"]: i for i, f in enumerate(files)}
    cm = CodeMap()
    for f in files:
        try:
            src = (repo / f["p"]).read_text(errors="ignore") if f.get("l") in ("python", "javascript", "typescript") else ""
        except OSError:
            src = ""
        cm.files.append({"p": f["p"], "m": f.get("n", ""), "t": f.get("name", ""), "d": f.get("d", ""), "x": externals(src), "loc": f.get("loc", 0)})
    for link in g["links"]:
        a, b = link["a"], link["b"]
        if a.startswith("f:") and b.startswith("f:") and a[2:] in index and b[2:] in index:
            cm.links.append([index[a[2:]], index[b[2:]], {"import": 0, "http": 1, "queue": 2}[link["k"]], (link.get("labels") or [""])[0]])
    cm.routes = [{"m": r["m"], "p": r["p"], "t": r.get("name", ""), "d": r.get("d", ""), "f": index[r["f"]], "l": r.get("l", 0)} for r in g["routes"] if r["f"] in index]
    cm.modules = [{"id": n["id"], "parent": n["parent"], "label": n["label"], "kind": n.get("kind", ""), "summary": n.get("summary", ""),
                   "paths": n.get("paths", []), "files": n.get("stats", {}).get("files", 0), "loc": n.get("stats", {}).get("loc", 0)} for n in g["nodes"]]
    # Floorplan modules are logical; deployable services are still detected from manifests.
    cm.services = sorted({str(r.relative_to(repo)) for r in service_roots(repo, [repo / f["p"] for f in files])} - {"."})
    cm.flows = g.get("flows", [])
    cm.module_links = [{"a": ln["a"][2:], "b": ln["b"][2:], "k": ln["k"], "label": (ln.get("labels") or [""])[0], "desc": ln.get("desc", "")}
                       for ln in g["links"] if ln["a"].startswith("n:") and ln["b"].startswith("n:")]
    return cm


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", action="append", required=True, help="repository folder (repeatable)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-floorplan", action="store_true", help="ignore floorplan/graph.json even if present")
    a = ap.parse_args()
    repos = [Path(r).expanduser().resolve() for r in a.repo]
    fp = repos[0] / "floorplan" / "graph.json"
    if len(repos) == 1 and fp.exists() and not a.no_floorplan:
        cm, source = from_floorplan(fp, repos[0]), "floorplan map"
    else:
        cm, source = scan(repos), "built-in scanner"
    base = repos[0] if len(repos) == 1 else Path(os.path.commonpath([str(r) for r in repos]))
    out = {"generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": source, "base": str(base),
           "services": cm.services, "files": cm.files, "links": cm.links, "routes": cm.routes, "modules": cm.modules, "moduleLinks": cm.module_links, "flows": cm.flows}
    Path(a.out).write_text(json.dumps(out, separators=(",", ":")))
    print(f"code map ({source}): {len(cm.services)} services, {len(cm.files)} files, {len(cm.routes)} APIs, {len(cm.links)} links -> {a.out}")


if __name__ == "__main__":
    main()
