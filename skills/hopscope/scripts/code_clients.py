"""Frontend -> backend links for the code map: which files are frontends, their HTTP calls (from each language's
client_calls hook), and the route each call reaches. Writes CodeMap.client_calls rows:
[file index, line, method, path, route index or None], plus "suffix" when matched by the path's tail only."""
from __future__ import annotations

import re
from pathlib import Path

import langs
from langs import clientmatch

FRONTEND_DIRS = {"frontend", "web", "ui", "client", "admin_frontend", "webapp", "web-app", "spa"}
FRONTEND_DEPS = re.compile(r'"(react|react-dom|vue|svelte|@sveltejs/kit|next|nuxt|@angular/core|solid-js|preact|@remix-run/react)"\s*:')
SERVER_DEPS = re.compile(r'"(express|fastify|@fastify/[\w.-]+|koa|@nestjs/core|@hapi/hapi|hono|restify|next-connect|h3|elysia|polka|@trpc/server)"\s*:')


def is_ui_package(root: Path) -> bool:
    """A package.json naming a UI framework and no server framework."""
    try:
        text = (root / "package.json").read_text(errors="ignore")
    except OSError:
        return False
    return bool(FRONTEND_DEPS.search(text)) and not SERVER_DEPS.search(text)


def attach(cm, all_files: list[tuple[Path, Path]], file_sid: list[str], sid_root: dict[str, Path], index: dict[str, int]) -> None:
    """Frontend files are those of a UI package or in a folder named like a frontend (a route-less Node worker is not
    one). In a UI package a route-looking api.get('/x') without a handler is a call, not a route; elsewhere a call at a
    route's line stays a route."""
    by_ext = langs.by_suffix()
    ui = {sid for sid, root in sid_root.items() if is_ui_package(root)}
    calls: list[list] = []
    for i, (f, repo) in enumerate(all_files):
        lang = by_ext[f.suffix]
        if not hasattr(lang, "client_calls"):
            continue
        folders = {p.lower() for p in cm.files[i]["p"].split("/")[:-1]}
        if file_sid[i] not in ui and not folders & FRONTEND_DIRS:
            continue
        ctx = langs.Context(index=index, root=sid_root.get(file_sid[i], repo), repo=repo)
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        calls += [[i, line, method, path] for line, method, path in lang.client_calls(f, text, ctx)]
    routes_at = {(r["f"], r["l"]) for r in cm.routes}
    calls = [c for c in calls if file_sid[c[0]] in ui or (c[0], c[1]) not in routes_at]
    at = {(c[0], c[1]) for c in calls}
    cm.routes = [r for r in cm.routes if (r["f"], r["l"]) not in at]
    cm.client_calls = []
    for c in calls:
        admin = "admin" in cm.files[c[0]]["p"].lower()
        idx, basis = clientmatch.match_with_basis(c[2], c[3], cm.routes, admin=admin)
        cm.client_calls.append([*c, idx, basis] if basis else [*c, idx])
