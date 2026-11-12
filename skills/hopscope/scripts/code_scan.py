"""Map one or more code repositories: services, files, API routes, imports and the external
systems each file talks to. Writes code.json (the console's code map).

    python code_scan.py --repo ~/src/backend --repo ~/src/web --out data/code.json

If a repo already has a floorplan map (floorplan/graph.json), its human-readable module and file
names are used; otherwise the built-in scanner reads Python, JavaScript/TypeScript and Go.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import code_clients
import langs
from langs.common import MAX_FILE_BYTES, humanize, read_contained, shared_externals

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", ".next", "site-packages", ".tox", ".mypy_cache",
             "coverage", "vendor", ".terraform", "migrations", "alembic", "tests", "test", "__tests__", "floorplan"}
LANG_BY_EXT = langs.by_suffix()
CODE_EXT = {ext: lang.NAME for ext, lang in LANG_BY_EXT.items()}
SERVICE_MARKERS = langs.service_markers()
BUILD_DIRS = langs.build_dirs()

METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "WS", "ANY"}
TEST_NAME = re.compile(r"(\.test|\.spec|_test)\.\w+$|\.d\.ts$|\.min\.js$")
__all__ = ["humanize", "scan", "from_floorplan", "service_roots", "externals"]


@dataclass
class CodeMap:
    files: list[dict] = field(default_factory=list)
    routes: list[dict] = field(default_factory=list)
    links: list[list] = field(default_factory=list)
    modules: list[dict] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    flows: list[dict] = field(default_factory=list)
    module_links: list[dict] = field(default_factory=list)
    client_calls: list[list] = field(default_factory=list)


def _is_test(name: str, lang) -> bool:
    extra = getattr(lang, "TEST_FILE", None)
    return bool(TEST_NAME.search(name) or (extra is not None and extra.search(name)))


@lru_cache(maxsize=4096)
def _listing(d: Path) -> frozenset[str]:
    try:
        return frozenset(os.listdir(d))
    except OSError:
        return frozenset()


def has_marker(d: Path, markers: tuple[str, ...] = SERVICE_MARKERS) -> bool:
    names = _listing(d)
    for m in markers:
        if "*" in m:
            if any(fnmatch.fnmatchcase(n, m) for n in names):
                return True
        elif m in names:
            return True
    return False


def _skip_dir(parent: Path, d: str) -> bool:
    if d in SKIP_DIRS or d.startswith("."):
        return True
    return d in BUILD_DIRS and has_marker(parent, BUILD_DIRS[d])


def _inside(p: Path, repo: Path) -> bool:
    """A file to read: not a symlink that leads out of the repository."""
    try:
        return not p.is_symlink() or p.resolve().is_relative_to(repo.resolve())
    except (OSError, RuntimeError):
        return False


def walk(repo: Path):
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if not _skip_dir(Path(dirpath), d)]
        for f in filenames:
            p = Path(dirpath) / f
            lang = LANG_BY_EXT.get(p.suffix)
            if lang is None or _is_test(f, lang) or not _inside(p, repo):
                continue
            if p.stat().st_size < MAX_FILE_BYTES:
                yield p


def service_roots(repo: Path, files: list[Path]) -> list[Path]:
    """Folders that look like deployable services: nearest ancestor with a Dockerfile or package manifest."""
    roots = set()
    for f in files:
        d = f.parent
        while True:
            if has_marker(d):
                roots.add(d)
                break
            if d == repo or d == d.parent:
                roots.add(repo)
                break
            d = d.parent
    return sorted(roots, key=lambda p: (len(p.parts), str(p)))


def owning_root(path: Path, roots: list[Path]) -> Path:
    best = None
    for r in roots:
        if path == r or r in path.parents:
            if best is None or len(r.parts) > len(best.parts):
                best = r
    return best or roots[0]


def externals(src: str) -> list[str]:
    """External systems any language shares (used for floorplan maps, whose files are not re-scanned per language)."""
    return sorted(shared_externals(src))


def full_prefix(i: int, mounted_by: dict[int, list[tuple[int, str]]], seen: frozenset = frozenset()) -> str:
    """Prefix a file's routes inherit from the files that mount it (first mount wins)."""
    if i not in mounted_by or i in seen:
        return ""
    parent, prefix = mounted_by[i][0]
    return full_prefix(parent, mounted_by, seen | {i}) + prefix


def all_prefixes(i: int, mounted_by: dict[int, list[tuple[int, str]]], seen: frozenset = frozenset()) -> list[str]:
    """Every prefix a file's routes get, one per mount path (an Express router mounted twice serves both)."""
    if i not in mounted_by or i in seen:
        return [""]
    out = [up + prefix for parent, prefix in mounted_by[i] for up in all_prefixes(parent, mounted_by, seen | {i})]
    return sorted(set(out))


def _join(pre: str, path: str) -> str:
    return (pre.rstrip("/") + "/" + path.lstrip("/")).rstrip("/") or "/" if pre else path


def _read(f: Path) -> str:
    return f.read_text(errors="ignore")


def _prepare(all_files: list[tuple[Path, Path]], texts: dict[int, str]) -> dict[str, object]:
    """Per-language state (e.g. a package index) for languages that need every file before resolving imports."""
    state: dict[str, object] = {}
    for lang in langs.LANGUAGES:
        if not hasattr(lang, "prepare"):
            continue
        mine = [i for i, (f, _) in enumerate(all_files) if LANG_BY_EXT[f.suffix] is lang]
        for i in mine:
            texts.setdefault(i, _read(all_files[i][0]))
        state[lang.NAME] = lang.prepare([(i, all_files[i][0], texts[i]) for i in mine])
    return state


def _modules(cm: CodeMap, module_stats: dict[str, dict]) -> None:
    for sid in cm.services:
        mods = {k: v for k, v in module_stats.items() if v["service"] == sid}
        cm.modules.append({"id": sid, "parent": None, "label": humanize(Path(sid).name), "kind": "service", "summary": "",
                           "paths": [sid + "/**"], "files": sum(v["files"] for v in mods.values()), "loc": sum(v["loc"] for v in mods.values())})
        for mid, v in mods.items():
            name = mid.split("::")[1]
            cm.modules.append({"id": mid, "parent": sid, "label": humanize(name) if name != "(root)" else "Entry points", "kind": "module", "summary": "",
                               "paths": [f"{sid}/{name}/**"], "files": v["files"], "loc": v["loc"]})


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
    texts: dict[int, str] = {}
    file_sid: list[str] = []
    state = _prepare(all_files, texts)
    for i, (f, repo) in enumerate(all_files):
        src = texts.pop(i, None)
        src = _read(f) if src is None else src
        root = owning_root(f, roots)
        sid = root_ids[str(root)]
        file_sid.append(sid)
        sub = f.relative_to(root).parts
        mid = f"{sid}::{sub[0] if len(sub) > 1 else '(root)'}"
        lang = LANG_BY_EXT[f.suffix]
        ctx = langs.Context(index=index, root=root, repo=repo, state=state.get(lang.NAME))
        routes, links = lang.routes(f, src, ctx), lang.imports(f, src, ctx)
        if hasattr(lang, "mounts"):
            for child, prefix in lang.mounts(f, src, ctx):
                mounted_by.setdefault(child, []).append((i, prefix))
        cm.files.append({"p": rel(f), "m": mid, "t": humanize(f.stem), "d": "", "x": sorted(lang.externals(src, ctx)), "loc": src.count("\n") + 1})
        for r in routes:
            if r["m"] in METHODS:
                cm.routes.append({**r, "f": i})
        cm.links += [[i, j, 0, ""] for j in links if j != i]
        st = module_stats.setdefault(mid, {"files": 0, "loc": 0, "service": sid})
        st["files"] += 1
        st["loc"] += src.count("\n") + 1
    prefixed: list[dict] = []
    for r in cm.routes:  # apply router prefixes now that every mount is known (Python: first mount, as before)
        if LANG_BY_EXT[all_files[r["f"]][0].suffix].NAME == "python":
            prefixed.append({**r, "p": _join(full_prefix(r["f"], mounted_by), r["p"])})
        else:
            prefixed += [{**r, "p": _join(pre, r["p"])} for pre in all_prefixes(r["f"], mounted_by)]
    cm.routes = prefixed
    _modules(cm, module_stats)
    code_clients.attach(cm, all_files, file_sid, {sid: Path(r) for r, sid in root_ids.items()}, index)
    return cm


def from_floorplan(graph_path: Path, repo: Path) -> CodeMap:
    """Use an existing floorplan map: better names and descriptions than the raw scan."""
    g = json.loads(graph_path.read_text())
    files = g["files"]
    index = {f["p"]: i for i, f in enumerate(files)}
    cm = CodeMap()
    root = repo.resolve()
    for f in files:
        src = (read_contained(root, str(f["p"])) or "") if f.get("l") in ("python", "javascript", "typescript") else ""
        cm.files.append({"p": f["p"], "m": f.get("n", ""), "t": f.get("name", ""), "d": f.get("d", ""), "x": externals(src), "loc": f.get("loc", 0)})
    for link in g["links"]:
        a, b = link["a"], link["b"]
        if a.startswith("f:") and b.startswith("f:") and a[2:] in index and b[2:] in index:
            cm.links.append([index[a[2:]], index[b[2:]], {"import": 0, "http": 1, "queue": 2}[link["k"]], (link.get("labels") or [""])[0]])
    cm.routes = [{"m": str(r["m"]).upper(), "p": r["p"], "t": r.get("name", ""), "d": r.get("d", ""), "f": index[r["f"]], "l": r.get("l", 0)}
                 for r in g["routes"] if r["f"] in index and str(r["m"]).upper() in METHODS]
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
           "services": cm.services, "files": cm.files, "links": cm.links, "routes": cm.routes, "modules": cm.modules, "moduleLinks": cm.module_links, "flows": cm.flows,
           "clientCalls": cm.client_calls}
    Path(a.out).write_text(json.dumps(out, separators=(",", ":")))
    matched = sum(1 for c in cm.client_calls if c[4] is not None)
    by_tail = sum(1 for c in cm.client_calls if len(c) > 5)
    calls = f", {len(cm.client_calls)} frontend calls ({matched} matched, {by_tail} by path tail)" if cm.client_calls else ""
    print(f"code map ({source}): {len(cm.services)} services, {len(cm.files)} files, {len(cm.routes)} APIs, {len(cm.links)} links{calls} -> {a.out}")


if __name__ == "__main__":
    main()
