"""Answer questions about a built Hopscope workdir: routes, traces, callers, change impact, resources, health.

Pure functions over the JSON files `hopscope.py all|design` writes (code.json, flows.json, model.json,
aws.json, and links.json when present). Standard library only: no AWS calls, no subprocesses, no network,
no writes. Every value read from the files is treated as untrusted text; strings are capped at
MAX_STR characters and lists at MAX_ITEMS entries so a large account can't flood an agent's context.

    data = load(".hopscope/data")
    impact(data, "app/identity.py")
"""
from __future__ import annotations

import json
import re
from collections import deque
from pathlib import Path
from typing import Any

MAX_STR, MAX_ITEMS, MAX_DEPTH = 300, 50, 6
RUNS = {0: "always", 1: "conditional", 2: "on error"}
LINK_KINDS = {0: "import", 1: "http", 2: "queue"}
METRIC_FIELDS = ("cpu", "mem", "memMax", "conns", "credits", "creditMin", "surplus")
FILES = ("code.json", "flows.json", "model.json", "aws.json", "links.json", "changes.json", "drift.json")


class NoData(Exception):
    """The workdir holds no built data yet."""


def clip(s: Any) -> str:
    return str(s if s is not None else "")[:MAX_STR]


def load(data_dir: str | Path) -> dict:
    """Read the JSON files in `data_dir` (a workdir's data/ or site/ folder)."""
    d = Path(data_dir)
    out: dict[str, Any] = {}
    for name in FILES:
        p = d / name
        if p.is_file():
            try:
                out[name[:-5]] = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise NoData(f"{name} in {d} could not be read ({type(e).__name__}); re-run hopscope.py design or all") from e
    if "code" not in out or "model" not in out:
        raise NoData(f"no Hopscope data in {d}: run hopscope.py design or all first")
    for key in ("flows", "aws", "links", "changes", "drift"):
        out.setdefault(key, {})
    out["paths"] = [str(f.get("p", "")) for f in out["code"].get("files", []) if isinstance(f, dict)]
    return out


# ---- small lookups -------------------------------------------------------------------------------

def _paths(data: dict) -> list[str]:
    """File paths by index (computed once in load)."""
    if "paths" not in data:
        data["paths"] = [str(f.get("p", "")) for f in data["code"].get("files", [])]
    return data["paths"]


def service_of(data: dict, path: str) -> str | None:
    """Deepest code folder (service) holding `path`."""
    for s in sorted(data["code"].get("services", []), key=len, reverse=True):
        if s == "." or path == s or path.startswith(s + "/"):
            return s
    return None


def _links_view(data: dict, key: str) -> dict:
    """model.json carries the linker's view for the console; links.json is the fuller original."""
    m = data["model"].get(key)
    return m if isinstance(m, dict) and m else (data["links"].get(key) or {})


def compute_for(data: dict, service: str | None) -> list[str]:
    if not service:
        return []
    placed = data["model"].get("placement", {}).get(service)
    if placed:
        return [clip(c) for c in placed.get("compute", [])[:MAX_ITEMS]]
    return [clip(c) for c in data["links"].get("codeToCompute", {}).get(service, [])[:MAX_ITEMS]]


# ---- how sure a code-to-compute link is ----------------------------------------------------------

CONFIRMED = 0.6  # the linker's review threshold: links below it are guesses a person should confirm
REVIEW_HINT = "confirm with hopscope.py review"
UNCONFIRMED_NOTE = ("Links marked unconfirmed were guessed by the linker with low confidence: say they are unconfirmed, "
                    "not fact, and suggest confirming them with hopscope.py review (or setting them in hopscope.json).")


def as_confidence(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return round(float(v), 2)


def _link_keys(data: dict, name: str, service: str | None) -> list[str]:
    row = _infra_by_name(data).get(name) or {}
    if row.get("key"):
        return [str(row["key"])]
    placement = data["links"].get("placement")
    if not isinstance(placement, dict):
        return []
    return [k for k, v in placement.items() if str(k).split("/", 1)[-1] == name and (not service or (isinstance(v, dict) and v.get("code") == service))]


def link_confidence(data: dict, name: str, service: str | None = None) -> float | None:
    """The linker's confidence that compute `name` runs its code (links.json, else the review list in model.json); None if unknown."""
    placement = data["links"].get("placement")
    reviews = [r for src in (data["links"].get("review"), data["model"].get("review")) if isinstance(src, list) for r in src if isinstance(r, dict)]
    for key in _link_keys(data, name, service):
        p = placement.get(key) if isinstance(placement, dict) else None
        if isinstance(p, dict) and as_confidence(p.get("confidence")) is not None:
            return as_confidence(p.get("confidence"))
        for r in reviews:
            if r.get("ecs") == key and as_confidence(r.get("confidence")) is not None:
                return as_confidence(r.get("confidence"))
    return None


def unconfirmed(conf: float | None) -> bool:
    return conf is not None and conf < CONFIRMED


def compute_links(data: dict, service: str | None) -> list[Any]:
    """compute_for, with each low-confidence link as {"name", "confidence", "unconfirmed": true}; confirmed ones stay names."""
    out: list[Any] = []
    for c in compute_for(data, service):
        conf = link_confidence(data, c, service)
        out.append({"name": c, "confidence": conf, "unconfirmed": True} if unconfirmed(conf) else c)
    return out


def compute_name(c: Any) -> str:
    """The name of a compute entry from compute_links (a name, or a dict for an unconfirmed link)."""
    return clip(c.get("name")) if isinstance(c, dict) else clip(c)


def _any_unconfirmed(items: list[Any]) -> bool:
    return any(isinstance(c, dict) and c.get("unconfirmed") for c in items)


def _route_name(r: dict) -> str:
    return clip(f'{r.get("m", "")} {r.get("p", "")}'.strip())


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]*\}|:[A-Za-z_]\w*", "{}", path.strip().lower().rstrip("/")) or "/"


def _route_index(data: dict, route: str | int) -> int | None:
    routes = data["code"].get("routes", [])
    s = str(route).strip()
    if s.isdigit():
        return int(s) if int(s) < len(routes) else None
    parts = s.split(None, 1)
    method, path = (parts[0].upper(), parts[1]) if len(parts) == 2 else ("", s)
    for i, r in enumerate(routes):
        if _norm(r.get("p", "")) == _norm(path) and (not method or r.get("m", "").upper() == method):
            return i
    return None


def _at(data: dict, file_idx: Any, line: Any) -> str:
    paths = _paths(data)
    p = paths[file_idx] if isinstance(file_idx, int) and 0 <= file_idx < len(paths) else "?"
    return clip(f"{p}:{line}")


def _steps(data: dict, idx: int) -> list[list]:
    return data["flows"].get(str(idx)) or []


def _infra_by_name(data: dict) -> dict[str, dict]:
    return {r.get("n"): r for r in data["model"].get("infra", [])}


def _is_publish(s: list) -> bool:
    return len(s) > 2 and (s[1] == "queue" or str(s[2]).startswith(("SQS: SendMessage", "SNS: Publish")))


def _publishes(steps: list[list]) -> bool:
    return any(_is_publish(s) for s in steps)


def _stem(queue: str) -> str:
    return re.sub(r"\.fifo$", "", queue)


def _queues_reached(data: dict, service: str | None, steps: list[list]) -> list[dict]:
    """Queues this route's own steps publish to, with the worker consuming each. A step that names its queue counts
    exactly ("basis": "step"); when a publishing step doesn't, the service's queues stand in ("service publishes to")."""
    if not service or not _publishes(steps):
        return []
    workers = _links_view(data, "queueWorkers")
    own = set(compute_for(data, service))
    candidates = [n for n in sorted(_links_view(data, "queues").get(service, []))
                  if (workers.get(n) or [None])[0] not in own]  # queues this service consumes are not published by it
    named: list[str] = []
    unnamed = False
    for st in steps:
        if not _is_publish(st):
            continue
        text = " ".join(str(x) for x in (st[2], *(st[6:8] if len(st) > 6 else ())))
        hits = [n for n in candidates if _stem(n) and _stem(n) in text]
        named += [n for n in hits if n not in named]
        unnamed = unnamed or not hits
    picks = [(n, "step") for n in named] + ([(n, "service publishes to") for n in candidates if n not in named] if unnamed else [])
    out = []
    for name, basis in picks:
        w = workers.get(name) or []
        out.append({"queue": clip(name), "worker": clip(w[0]) if w else None, "job": clip(w[1]) if len(w) > 1 else None, "basis": basis})
    return out[:MAX_ITEMS]


def _database(data: dict, service: str | None, steps: list[list]) -> str | None:
    db = _links_view(data, "dbFor").get(service or "")
    if db:
        return clip(db)
    if any(len(s) > 1 and s[1] == "sql" for s in steps) and data["model"].get("primaryDb"):
        return clip(data["model"]["primaryDb"])
    return None


def _entry(data: dict, compute: list[str]) -> list[dict]:
    """Load balancer rules that send traffic to the compute a route runs on."""
    infra = _infra_by_name(data)
    keys = {infra[c]["key"] for c in compute if c in infra and infra[c].get("key")}
    out = []
    for lb in data["aws"].get("lb", []) or []:
        for r in lb.get("routes", []):
            if keys & set(r.get("services", [])):
                out.append({"lb": clip(lb.get("n")), "when": clip(r.get("when")), "target": clip(r.get("tg"))})
    return out[:MAX_ITEMS]


# ---- the questions -------------------------------------------------------------------------------

def _segments(path: str) -> list[str]:
    return [s for s in path.lower().split("/") if s]


def _word_score(w: str, method: str, path: str, title: str) -> int:
    """Whole path segments count most (the last one a little more), then words inside a segment, the title
    and finally any substring of the path."""
    if w == method:
        return 2
    segs = _segments(path)
    if "/" in w:
        return 5 if _norm(w) == _norm(path) else 3 if w in path else 0
    if w in segs:
        return 6 + (1 if segs[-1] == w else 0)
    if any(w in re.split(r"[-_.:{}]+", s) for s in segs):
        return 4
    if w in re.split(r"\W+", title):
        return 2
    return 1 if w in path else 0


def find_routes(data: dict, q: str, limit: int = 10) -> dict:
    """Routes matching a method, path or title words, best first."""
    q = clip(q)
    limit = max(1, min(int(limit or 10), MAX_ITEMS))
    words = [w for w in re.split(r"[^a-z0-9{}:/_.-]+", q.lower()) if w]
    exact = _route_index(data, q)
    scored = []
    for i, r in enumerate(data["code"].get("routes", [])):
        method, path, title = r.get("m", "").lower(), r.get("p", "").lower(), r.get("t", "").lower()
        score = 100 if i == exact else 0
        score += sum(_word_score(w, method, path, title) for w in words)
        if score:
            scored.append((-score, i, r))
    scored.sort(key=lambda x: (x[0], x[1]))
    paths = _paths(data)
    return {"query": q, "routes": [{"route": _route_name(r), "title": clip(r.get("t")), "handler": _at(data, r.get("f"), r.get("l")),
                                    "service": service_of(data, paths[r["f"]]) if 0 <= r.get("f", -1) < len(paths) else None, "score": -s}
                                   for s, _, r in scored[:limit]]}


CLEAR_MIN = 4  # at least a word of the path


def _best_free_text(data: dict, route: str) -> tuple[int | None, list[dict]]:
    """For a free-text query (no path given): the route index when one match is clearly best, else the candidates."""
    found = find_routes(data, route, 10)["routes"]
    if "/" in route or not found:
        return None, found[:5]
    top = found[0]["score"]
    if top >= CLEAR_MIN and (len(found) == 1 or found[1]["score"] < top):
        return _route_index(data, found[0]["route"]), found[:5]
    return None, [f for f in found if f["score"] == top] or found[:5]


def trace(data: dict, route: str | int) -> dict:
    """One route's code path in order, the compute it runs on, and the queues, workers and database it reaches.
    A free-text query ('checkout') follows the clearly best matching route, else lists the close candidates."""
    idx = _route_index(data, route)
    matched = ""
    if idx is None:
        idx, found = _best_free_text(data, str(route))
        if idx is None:
            close = len(found) > 1 and "/" not in str(route) and found[0]["score"] >= CLEAR_MIN
            msg = f"{len(found)} routes match {clip(route)!r}" if close else f"no route matches {clip(route)!r}"
            return {"error": msg, "suggestions": found, "ambiguous": close}
        matched = f"best match for {clip(route)!r}"
    r = data["code"]["routes"][idx]
    paths = _paths(data)
    handler_path = paths[r["f"]] if 0 <= r.get("f", -1) < len(paths) else ""
    service = service_of(data, handler_path)
    steps = _steps(data, idx)
    compute = compute_for(data, service)
    shown = compute_links(data, service)
    out_steps = [{"depth": s[0], "kind": clip(s[1]), "label": clip(s[2]), "at": _at(data, s[3], s[4]), "runs": RUNS.get(s[5], "always"),
                  "when": clip(s[6] if len(s) > 6 else ""), "why": clip(s[7] if len(s) > 7 else "")} for s in steps[:MAX_ITEMS]]
    return {"route": _route_name(r), "title": clip(r.get("t")), "handler": _at(data, r.get("f"), r.get("l")), "service": service,
            "compute": shown, "entry": _entry(data, compute), "steps": out_steps,
            "traced": bool(steps) or handler_path.endswith(".py"),
            "queues": _queues_reached(data, service, steps), "database": _database(data, service, steps),
            "reaches": sorted({clip(s[1]) for s in steps if len(s) > 1 and s[1] != "call"}), **({"matched": matched} if matched else {}),
            **({"note": UNCONFIRMED_NOTE} if _any_unconfirmed(shown) else {})}


def _match_files(data: dict, target: str) -> list[int]:
    """Files a target names, from the mapped paths only (never the filesystem). Accepts repo-relative paths, their
    tails (app/db.py), absolute or Windows paths that end in a mapped path, and folders (whole segments only)."""
    t = target.strip().replace("\\", "/")
    while t.startswith("./"):
        t = t[2:]
    folder = t.endswith("/")
    t = t.strip("/")
    if not t:
        return []
    paths = _paths(data)
    if not folder:
        for test in (lambda p: p == t, lambda p: p.endswith("/" + t), lambda p: t.endswith("/" + p)):
            hits = [i for i, p in enumerate(paths) if p and test(p)]
            if hits:
                return hits
    segs = t.split("/")
    for start in range(len(segs)):  # longest suffix first, so /home/me/proj/svc/app matches svc/app
        tail = "/".join(segs[start:])
        hits = [i for i, p in enumerate(paths) if p.startswith(tail + "/") or (start == 0 and ("/" + tail + "/") in ("/" + p))]
        if hits:
            return hits
    return []


def _reverse_walk(data: dict, start: list[int], depth: int) -> dict[int, tuple[int, str]]:
    """Files that depend on `start` (import, HTTP or queue edges, followed backwards): {file: (depth, kind)}."""
    rev: dict[int, list[tuple[int, int]]] = {}
    for link in data["code"].get("links", []):
        if len(link) >= 3 and link[0] != link[1]:
            rev.setdefault(link[1], []).append((link[0], link[2]))
    seen: dict[int, tuple[int, str]] = {i: (0, "") for i in start}
    todo = deque(start)
    while todo:
        cur = todo.popleft()
        d = seen[cur][0]
        if d >= depth:
            continue
        for src, kind in sorted(rev.get(cur, [])):
            if src not in seen:
                seen[src] = (d + 1, LINK_KINDS.get(kind, "import"))
                todo.append(src)
    return seen


def _routes_touching(data: dict, dist: dict[int, int]) -> list[dict]:
    """Routes whose handler or any flow step sits in a file of `dist` ({file: distance}), nearest first."""
    out = []
    for i, r in enumerate(data["code"].get("routes", [])):
        best, why = None, ""
        if r.get("f") in dist:
            best, why = dist[r["f"]], f'handler in {_at(data, r["f"], r.get("l"))}'
        for s in _steps(data, i):
            if len(s) > 4 and s[3] in dist and (best is None or dist[s[3]] < best):
                best, why = dist[s[3]], f"{clip(s[2])} at {_at(data, s[3], s[4])}"
        if best is not None:
            out.append({"index": i, "route": _route_name(r), "distance": best, "reason": why})
    out.sort(key=lambda x: (x["distance"], x["index"]))
    return out


def who_calls(data: dict, target: str, depth: int = 3) -> dict:
    """Files that import or call a file (or the file defining a function), transitively, and routes that reach it."""
    target = clip(target)
    depth = max(1, min(int(depth or 3), MAX_DEPTH))
    files = _match_files(data, target)
    if not files:
        name = target.split(".")[-1].rstrip("()")
        files = sorted({s[3] for steps in data["flows"].values() for s in steps if len(s) > 3 and s[2] == f"{name}()" and isinstance(s[3], int) and s[3] >= 0})
        symbol = bool(files)
    else:
        symbol = False
    if not files:
        return {"error": f"no file or function matches {target!r}"}
    paths = _paths(data)
    walk = _reverse_walk(data, files, depth)
    callers = sorted(((d, paths[i], k) for i, (d, k) in walk.items() if d > 0), key=lambda x: (x[0], x[1]))
    if symbol:
        call = target.split(".")[-1].rstrip("()") + "()"
        routes = []
        for i, r in enumerate(data["code"].get("routes", [])):
            hit = next((s for s in _steps(data, i) if len(s) > 4 and s[2] == call), None)
            if hit:
                routes.append({"route": _route_name(r), "distance": 0, "reason": f"calls {call} at {_at(data, hit[3], hit[4])}"})
    else:
        routes = [{k: v for k, v in r.items() if k != "index"} for r in _routes_touching(data, {i: d for i, (d, _) in walk.items()})]
    return {"target": target, "files": [paths[i] for i in files][:MAX_ITEMS],
            "callers": [{"file": p, "depth": d, "via": k} for d, p, k in callers][:MAX_ITEMS], "routes": routes[:MAX_ITEMS]}


def what_runs(data: dict, code_folder: str) -> dict:
    """The compute a code folder runs on, its queues (and their workers), database and route count."""
    folder = clip(code_folder).strip().strip("/")
    service = folder if folder in data["code"].get("services", []) else service_of(data, folder)
    if not service:
        return {"error": f"{folder!r} is not a code folder Hopscope mapped", "folders": data["code"].get("services", [])[:MAX_ITEMS]}
    infra = _infra_by_name(data)
    compute = []
    for c in compute_for(data, service):
        conf = link_confidence(data, c, service)
        compute.append({"name": c, **{k: infra.get(c, {}).get(k) for k in ("kind", "env", "tasks", "size")},
                        **({"confidence": conf} if conf is not None else {}), **({"unconfirmed": True} if unconfirmed(conf) else {})})
    workers = _links_view(data, "queueWorkers")
    own = {c["name"] for c in compute}
    queues = [{"queue": clip(q), "role": "consumes" if (workers.get(q) or [None])[0] in own else "publishes",
               "worker": clip((workers.get(q) or [None])[0]) if workers.get(q) else None} for q in sorted(_links_view(data, "queues").get(service, []))]
    paths = _paths(data)
    n_routes = sum(1 for r in data["code"].get("routes", []) if 0 <= r.get("f", -1) < len(paths) and service_of(data, paths[r["f"]]) == service)
    return {"folder": folder, "service": service, "compute": compute[:MAX_ITEMS], "queues": queues[:MAX_ITEMS],
            "database": clip(_links_view(data, "dbFor").get(service)) or None, "routes": n_routes,
            "unplaced": not compute, **({"note": UNCONFIRMED_NOTE} if _any_unconfirmed(compute) else {})}


def impact(data: dict, path: str) -> dict:
    """What a change to `path` can break: dependent files, routes (nearest first), compute, queues and workers."""
    path = clip(path)
    files = _match_files(data, path)
    if not files:
        return {"error": f"no file matches {path!r}; paths are relative to the repo folders Hopscope scanned"}
    paths = _paths(data)
    walk = _reverse_walk(data, files, MAX_DEPTH)
    importers = sorted(((d, paths[i], k) for i, (d, k) in walk.items() if d > 0), key=lambda x: (x[0], x[1]))
    routes = _routes_touching(data, {i: d for i, (d, _) in walk.items()})
    services: dict[str, list[Any]] = {}
    queues: list[dict] = []
    for r in routes:
        svc = service_of(data, paths[data["code"]["routes"][r["index"]]["f"]])
        r["service"] = svc
        if svc:
            services.setdefault(svc, compute_links(data, svc))
        for qd in _queues_reached(data, svc, _steps(data, r["index"])):
            same = next((x for x in queues if x["queue"] == qd["queue"]), None)
            if same is None:
                queues.append(qd)
            elif qd["basis"] == "step":
                same["basis"] = "step"
    queues.sort(key=lambda x: x["queue"])
    workers = sorted({x["worker"] for x in queues if x["worker"]})
    direct = sum(1 for r in routes if r["distance"] == 0)
    summary = (f"{len(importers)} dependent file(s), {len(routes)} route(s) ({direct} step into the file directly), "
               f"{len(services)} service(s), {len(queues)} queue(s), {len(workers)} worker(s)")
    return {"path": path, "matched": [paths[i] for i in files][:MAX_ITEMS],
            "importers": [{"file": p, "depth": d, "via": k} for d, p, k in importers][:MAX_ITEMS],
            "routes": [{k: v for k, v in r.items() if k != "index"} for r in routes][:MAX_ITEMS],
            "services": [{"service": s, "compute": c} for s, c in sorted(services.items())][:MAX_ITEMS],
            "queues": queues[:MAX_ITEMS], "workers": workers[:MAX_ITEMS], "summary": summary,
            **({"note": UNCONFIRMED_NOTE} if any(_any_unconfirmed(c) for c in services.values()) else {})}


def _row(data: dict, r: dict, design: bool) -> dict:
    keep = ("n", "kind", "env", "sys", "size", "tasks", "cluster") + (() if design else METRIC_FIELDS)
    out = {k: (clip(r[k]) if isinstance(r[k], str) else r[k]) for k in keep if k in r}
    out["status"] = clip(r.get("lvl") or "ok")
    if r.get("note"):
        out["note"] = clip(r["note"])
    if r.get("kind") == "ecs" and r.get("key"):  # sys is the code folder it runs: say when that link is a guess
        conf = link_confidence(data, str(r.get("n")))
        if unconfirmed(conf):
            out.update(confidence=conf, unconfirmed=True)
    return out


def _aws_counts(aws: dict) -> dict[str, int]:
    out = {}
    for k, v in sorted(aws.items()):
        if k in ("collectedAt", "region", "account", "mode", "cost", "skipped"):
            continue
        n = len(v) if isinstance(v, list) else v.get("count", v.get("total", len(v))) if isinstance(v, dict) else v if isinstance(v, int) else None
        if isinstance(n, int):
            out[clip(k)] = n
        if len(out) >= MAX_ITEMS:
            break
    return out


def _mode(data: dict) -> str:
    return clip(data["model"].get("meta", {}).get("mode") or data["aws"].get("mode") or "live")


def resources(data: dict, kind: str | None = None) -> dict:
    """Infrastructure rows (optionally one kind: ecs, ec2, rds, sqs, …) and a count of every AWS component type."""
    design = _mode(data) == "design"
    rows = [r for r in data["model"].get("infra", []) if not kind or r.get("kind") == kind]
    kinds = sorted({clip(r.get("kind")) for r in data["model"].get("infra", [])})[:MAX_ITEMS]
    return {"mode": _mode(data), "kind": clip(kind) or None, "kinds": kinds, "total": len(rows),
            "rows": [_row(data, r, design) for r in rows[:MAX_ITEMS]], "aws": _aws_counts(data["aws"])}


def health(data: dict, name: str) -> dict:
    """Status and note for a resource (or code folder), plus the action-queue items that mention it."""
    name = clip(name).strip()
    design = _mode(data) == "design"
    infra = data["model"].get("infra", [])
    rows = [r for r in infra if r.get("n") == name] or [r for r in infra if name.lower() in str(r.get("n", "")).lower() or r.get("sys") == name]
    actions = [{"level": clip(a[0]), "title": clip(a[1]), "impact": clip(a[2]), "fix": clip(a[3])}
               for a in data["model"].get("actions", []) if len(a) >= 4 and name.lower() in " ".join(map(str, a[1:])).lower()]
    shown = [_row(data, r, design) for r in rows[:MAX_ITEMS]]
    out: dict[str, Any] = {"name": name, "mode": "design" if design else "live", "rows": shown,
                           "actions": actions[:MAX_ITEMS], "overall": {k: clip(v) for k, v in (data["model"].get("status") or {}).items()}}
    if any(r.get("unconfirmed") for r in shown):
        out["links"] = UNCONFIRMED_NOTE
    if design:
        out["note"] = "Design mode: built from code and infrastructure files, so there are no live numbers (traffic, errors, CPU, cost)."
    elif not rows and not actions:
        out["note"] = f"nothing named {name!r} in the inventory"
    return out
