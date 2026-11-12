"""Frontend callers of API routes, from code.json's clientCalls: [file index, line, method, path, route index or None],
with a sixth item "suffix" when the call was matched by its path's tail only.

Used by hopscope_query.trace: a route's "calledFrom" (the frontend files and lines that call it), and a trace that starts
at a frontend file ("src/pages/Members.tsx" or "Members.tsx:42") instead of a route. Standard library only."""
from __future__ import annotations

import re
from typing import Any

MAX_STR, MAX_CALLERS = 300, 20


def _clip(s: Any) -> str:
    return str(s if s is not None else "")[:MAX_STR]


def calls(data: dict) -> list[tuple[int, int, str, str, int | None, str]]:
    """Well-formed clientCalls rows (code.json is data: anything else is ignored)."""
    paths = data.get("paths") or [str(f.get("p", "")) for f in data["code"].get("files", []) if isinstance(f, dict)]
    n_routes = len(data["code"].get("routes", []))
    out = []
    for c in data["code"].get("clientCalls") or []:
        if not (isinstance(c, list) and len(c) in (5, 6) and isinstance(c[0], int) and 0 <= c[0] < len(paths) and isinstance(c[1], int)):
            continue
        r = c[4] if isinstance(c[4], int) and 0 <= c[4] < n_routes else None
        out.append((c[0], c[1], str(c[2])[:10], str(c[3]), r, "suffix" if len(c) == 6 and c[5] == "suffix" else ""))
    return out


def _where(data: dict, c: tuple) -> str:
    paths = data.get("paths") or [str(f.get("p", "")) for f in data["code"].get("files", [])]
    return _clip(f"{paths[c[0]]}:{c[1]}")


def called_from(data: dict, route_index: int) -> list[str]:
    """Frontend file:line places that call this route."""
    return [_where(data, c) + (" (matched by path tail)" if c[5] else "") for c in calls(data) if c[4] == route_index][:MAX_CALLERS]


def _route_name(data: dict, i: int) -> str:
    r = data["code"]["routes"][i]
    return _clip(f'{r.get("m", "")} {r.get("p", "")}'.strip())


def route_for_file(data: dict, target: str) -> tuple[int | None, list[dict], str]:
    """A frontend file or file:line -> (the route it calls when there is exactly one, the routes it calls otherwise,
    an error when it calls no mapped route). (None, [], "") when the target names no frontend file."""
    m = re.match(r"^(.*?\.[A-Za-z]{2,4})(?::(\d+))?$", target.strip().replace("\\", "/"))
    if not m:
        return None, [], ""
    tail, line = m.group(1).lstrip("./"), int(m.group(2)) if m.group(2) else None
    paths = data.get("paths") or [str(f.get("p", "")) for f in data["code"].get("files", [])]
    hits = [c for c in calls(data) if (paths[c[0]] == tail or paths[c[0]].endswith("/" + tail)) and (line is None or c[1] == line)]
    if not hits:
        return None, [], ""
    routes = list(dict.fromkeys(c[4] for c in hits if c[4] is not None))
    if len(routes) == 1:
        return routes[0], [], ""
    if not routes:
        shown = "; ".join(f"{c[2]} {c[3]}" for c in hits[:5])
        return None, [], _clip(f"{target}: its calls reach no mapped route ({shown})")
    return None, [{"route": _route_name(data, r), "from": next(_where(data, c) for c in hits if c[4] == r)} for r in routes[:MAX_CALLERS]], ""
