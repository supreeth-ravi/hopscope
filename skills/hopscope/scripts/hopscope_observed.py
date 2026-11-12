"""Answers about browser captures (observed.json from `hopscope.py observe --har`): whether a route was seen and with
which status classes, and the captured requests no scanned route serves. Standard library only; every value read
from the file is untrusted text and capped."""
from __future__ import annotations

from typing import Any

MAX_STR, MAX_ITEMS = 300, 50
ORDER = ("2xx", "3xx", "4xx", "5xx", "failed")
HOW = ("No browser capture yet. In Chrome open DevTools > Network, use the app, then right-click the request list and choose "
       "'Save all as HAR with content' (or the download arrow); run hopscope.py observe --har <file> --workdir <workdir>.")


def _clip(s: Any) -> str:
    return str(s if s is not None else "")[:MAX_STR]


def _int(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _status(raw: Any) -> dict[str, int]:
    raw = raw if isinstance(raw, dict) else {}
    return {k: _int(raw.get(k)) for k in ORDER if _int(raw.get(k))}


def note(count: int, status: dict[str, int]) -> str:
    parts = ", ".join(f"{n} {k}" for k, n in status.items())
    return f"seen in a browser capture ({count} request{'s' if count != 1 else ''}{': ' + parts if parts else ''})"


def _observed(data: dict) -> dict:
    o = data.get("observed")
    return o if isinstance(o, dict) else {}


def _row(data: dict, idx: int) -> dict | None:
    """The capture row of a route: by index while the route there is the same one, else by its name (a rebuild can renumber)."""
    rows = _observed(data).get("routes")
    routes = data.get("code", {}).get("routes") or []
    if not isinstance(rows, dict) or not 0 <= idx < len(routes):
        return None
    name = f'{routes[idx].get("m", "")} {routes[idx].get("p", "")}'
    row = rows.get(str(idx))
    if isinstance(row, dict) and row.get("route") == name:
        return row
    return next((r for r in rows.values() if isinstance(r, dict) and r.get("route") == name), None)


def for_route(data: dict, idx: int) -> dict | None:
    row = _row(data, idx)
    if not isinstance(row, dict) or not _int(row.get("count")):
        return None
    status = _status(row.get("status"))
    avg = row.get("avgMs") if isinstance(row.get("avgMs"), (int, float)) else None
    return {"count": _int(row["count"]), "status": status, "avgMs": avg, "lastSeen": _clip(row.get("lastSeen"))[:40], "note": note(_int(row["count"]), status)}


def summary(data: dict) -> dict:
    o = _observed(data)
    if not o:
        return {"captures": 0, "routes": [], "unmatched": [], "note": HOW}
    routes: list[dict[str, Any]] = [{"route": _clip(r.get("route")), "count": _int(r.get("count")), "status": _status(r.get("status")), "note": note(_int(r.get("count")), _status(r.get("status")))}
              for r in (o.get("routes") or {}).values() if isinstance(r, dict)] if isinstance(o.get("routes"), dict) else []
    routes.sort(key=lambda r: (-_int(r["count"]), str(r["route"])))
    unmatched = []
    for u in o.get("unmatched") or []:
        if isinstance(u, dict):
            m, p, n = _clip(u.get("method"))[:10], _clip(u.get("path")), _int(u.get("count"))
            unmatched.append({"method": m, "path": p, "count": n, "status": _status(u.get("status")),
                              "text": f"the browser called {m} {p}, which no scanned route serves ({n} request{'s' if n != 1 else ''})"})
    return {"captures": _int(o.get("captures")), "requests": _int(o.get("requests")), "routes": routes[:MAX_ITEMS], "unmatched": unmatched[:MAX_ITEMS],
            "note": "Counted from browser captures (HAR) on this machine: only methods, path patterns, status classes and timings are kept."}
