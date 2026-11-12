"""Observed traffic, first step: browser captures (HAR files exported from the browser's developer tools) matched to the
scanned routes. Writes observed.json: per route, how many requests, their status classes, average duration and when
the last one was seen; and the captured requests that match no route.

    python observe_har.py --code data/code.json --har capture.har [--har …] [--include-host api.example.com] --out data/observed.json

A HAR holds cookies, tokens, headers and bodies. This reads it locally and keeps only the method, the path with every
id-like segment replaced by {id}, the status and the time: never headers, cookies, query strings, bodies, hosts or full
URLs. Static files (scripts, styles, images, fonts, page loads that match no route) and requests to other sites are
counted and dropped. A host is ours when it served the captured page, is named with --include-host, or served requests
matching at least two different scanned routes (one match, say GET /, proves nothing). A path segment is kept only when it
is a plain lowercase word or a literal segment of a scanned route; anything else (ids, tokens, names) becomes {id}."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from langs import clientmatch

MAX_HAR_BYTES = 100_000_000
MAX_UNMATCHED, MAX_SEGMENTS, MAX_PATH = 50, 20, 200
STATIC_TYPES = {"script", "stylesheet", "image", "font", "media", "manifest", "texttrack", "document", "websocket", "ping"}
STATIC_EXT = re.compile(r"\.(js|mjs|css|map|png|jpe?g|gif|svg|webp|avif|ico|woff2?|ttf|otf|eot|mp4|webm|mp3|wav|html?|txt|json\.gz|wasm)$", re.I)
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


class BadHar(ValueError):
    """Not a HAR file this can read."""


def read_har(path: Path) -> dict:
    if path.stat().st_size > MAX_HAR_BYTES:
        raise BadHar(f"{path.name} is larger than {MAX_HAR_BYTES // 1_000_000} MB")
    try:
        doc = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except ValueError as e:
        raise BadHar(f"{path.name} is not JSON") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("log"), dict) or not isinstance(doc["log"].get("entries"), list):
        raise BadHar(f"{path.name} has no log.entries: export it with 'Save all as HAR'")
    return doc


PLAIN_WORD = re.compile(r"^[a-z][a-z-]{0,29}$")


def pattern(path: str, literals: frozenset[str] = frozenset()) -> str:
    """/api/orders/1042/items -> /api/orders/{id}/items: a segment survives only as a plain lowercase word or a literal
    segment of a scanned route; numbers, uuids, tokens, mixed case and anything encoded become {id}."""
    segs = [s for s in path.split("/") if s][:MAX_SEGMENTS]
    return ("/" + "/".join(s if PLAIN_WORD.match(s) or s in literals else "{id}" for s in segs))[:MAX_PATH]


def route_literals(routes: list[dict]) -> frozenset[str]:
    return frozenset(s for r in routes for s in str(r.get("p", "")).split("/") if s and not clientmatch.PARAM.match(s) and "{" not in s and ":" not in s)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _when(v: Any) -> str:
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _status(code: Any) -> str:
    return f"{code // 100}xx" if isinstance(code, int) and 100 <= code < 600 else "failed"


def _requests(doc: dict) -> tuple[list[dict], set[str]]:
    """(method, path, host, status, ms, at, type) of each well-formed entry, and the hosts of the captured pages."""
    log: dict[str, Any] = doc["log"] if isinstance(doc.get("log"), dict) else {}
    pages = {_host(str(p.get("title") or "")) for p in log.get("pages") or [] if isinstance(p, dict)} - {""}
    out: list[dict[str, Any]] = []
    for e in log.get("entries") or []:
        req = e.get("request") if isinstance(e, dict) else None
        if not isinstance(req, dict) or not isinstance(req.get("url"), str) or str(req.get("method", "")).upper() not in METHODS:
            continue
        resp = e.get("response") if isinstance(e.get("response"), dict) else {}
        try:
            path = unquote(urlsplit(req["url"]).path) or "/"
        except ValueError:
            continue
        ms = e.get("time") if isinstance(e.get("time"), (int, float)) and e["time"] >= 0 else 0
        out.append({"m": str(req["method"]).upper(), "path": path, "host": _host(req["url"]), "status": _status(resp.get("status")), "ms": float(ms),
                    "at": _when(e.get("startedDateTime")), "type": str(e.get("_resourceType") or "")})
        if e.get("_resourceType") == "document" and not pages:
            pages.add(_host(req["url"]))
    return out, pages


def _add(bucket: dict, r: dict) -> None:
    bucket["count"] += 1
    bucket["status"][r["status"]] = bucket["status"].get(r["status"], 0) + 1
    bucket["ms"] += r["ms"]
    bucket["lastSeen"] = max(bucket["lastSeen"], r["at"])


def observe(docs: list[dict], routes: list[dict], include_hosts: set[str] | None = None) -> dict:
    reqs: list[dict] = []
    pages: set[str] = set()
    for d in docs:
        found, hosts = _requests(d)
        reqs += found
        pages |= hosts
    has_options = any(str(r.get("m", "")).upper() == "OPTIONS" for r in routes)
    skipped = {"static": 0, "otherHosts": 0, "preflight": 0}
    literals = route_literals(routes)
    for r in reqs:
        r["pattern"] = pattern(r["path"], literals)
        r["route"] = clientmatch.match_with_basis(r["m"], r["path"], routes)[0] if not (r["m"] == "OPTIONS" and not has_options) else None
    served: dict[str, set[int]] = {}
    for r in reqs:
        if r["route"] is not None:
            served.setdefault(r["host"], set()).add(r["route"])
    ours = pages | {h for h, rs in served.items() if len(rs) >= 2} | {h.lower() for h in include_hosts or ()}
    by_route: dict[int, dict] = {}
    unmatched: dict[tuple[str, str], dict] = {}
    for r in reqs:
        if r["host"] not in ours:
            skipped["otherHosts"] += 1
        elif r["m"] == "OPTIONS" and r["route"] is None:
            skipped["preflight"] += 1
        elif r["route"] is not None:
            _add(by_route.setdefault(r["route"], {"count": 0, "status": {}, "ms": 0.0, "lastSeen": ""}), r)
        elif r["type"] in STATIC_TYPES or STATIC_EXT.search(r["path"]):
            skipped["static"] += 1
        else:
            _add(unmatched.setdefault((r["m"], r["pattern"]), {"count": 0, "status": {}, "ms": 0.0, "lastSeen": ""}), r)
    order = ("2xx", "3xx", "4xx", "5xx", "failed")

    def status(b: dict) -> dict:
        return {k: b["status"][k] for k in order if k in b["status"]}

    rows = {str(i): {"route": f'{routes[i].get("m", "")} {routes[i].get("p", "")}'[:MAX_PATH + 10], "count": b["count"], "status": status(b),
                     "avgMs": round(b["ms"] / b["count"], 1), "lastSeen": b["lastSeen"]} for i, b in sorted(by_route.items())}
    misses = sorted(unmatched.items(), key=lambda kv: (-kv[1]["count"], kv[0]))[:MAX_UNMATCHED]
    return {"generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "captures": len(docs),
            "requests": sum(b["count"] for b in by_route.values()) + sum(b["count"] for b in unmatched.values()), "routes": rows,
            "unmatched": [{"method": m, "path": p, "count": b["count"], "status": status(b), "lastSeen": b["lastSeen"]} for (m, p), b in misses],
            "unmatchedMore": max(0, len(unmatched) - MAX_UNMATCHED), "skipped": skipped}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--code", required=True)
    ap.add_argument("--har", action="append", required=True, help="a HAR file exported from the browser (repeatable)")
    ap.add_argument("--include-host", action="append", default=[], help="also keep requests to this host (an API on another domain)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    routes = json.loads(Path(a.code).read_text(encoding="utf-8")).get("routes") or []
    try:
        docs = [read_har(Path(h).expanduser()) for h in a.har]
    except (BadHar, OSError) as e:
        raise SystemExit(f"observe: {e}") from None
    out = observe(docs, routes, set(a.include_host))
    Path(a.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    sk = out["skipped"]
    print(f"observed {out['requests']} requests in {out['captures']} capture(s): {len(out['routes'])} routes seen, {len(out['unmatched'])} request patterns "
          f"match no route ({sk['static']} static files, {sk['otherHosts']} requests to other sites skipped) -> {a.out}")
    for u in out["unmatched"][:10]:
        print(f"  the browser called {u['method']} {u['path']}, which no scanned route serves ({u['count']} requests)")


if __name__ == "__main__":
    main()
