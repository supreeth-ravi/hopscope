"""Match a frontend's HTTP call (method, path) to a backend route: same method (or a route serving ANY), the same
segments once parameters ({id}, :id, <id>, numbers) are alike and common prefixes (/api, /api/v1, /rest) are allowed to
differ, ignoring case. A literal segment in the call must match the route's literal or parameter; a parameter in the
call matches only a route parameter. The best-scoring route wins."""
from __future__ import annotations

import re

PREFIX = re.compile(r"^(api|v\d+(\.\d+)?|rest)$")
UNSURE = "{?}"  # a route path built from a constant the scanner couldn't resolve: never matched
PARAM = re.compile(r"^(\{.*\}|:\w+|<.*>|\*+|\d+|[0-9a-f]{8}-[0-9a-f-]{27,})$", re.I)


def segments(path: str) -> list[str]:
    return ["{}" if PARAM.match(s) or "{" in s else s.lower() for s in path.split("/") if s]


def _score(c: list[str], r: list[str]) -> int | None:
    if len(c) != len(r):
        return None
    total = 0
    for a, b in zip(c, r, strict=True):
        if a == b:
            total += 3 if a != "{}" else 2
        elif b == "{}":
            total += 1
        else:
            return None
    return total


def score(client: str, route: str) -> int | None:
    cs, rs = segments(client), segments(route)
    best = None
    for dc in range(0, 3):
        for dr in range(0, 3):
            if dc > len(cs) or dr > len(rs) or not all(PREFIX.match(s) for s in cs[:dc] + rs[:dr]):
                continue
            s = _score(cs[dc:], rs[dr:])
            if s is not None and (cs[dc:] or not cs):
                s -= dc + dr
                best = s if best is None else max(best, s)
    return best


def _plausible_head(head: list[str]) -> bool:
    """What a client's base URL may have hidden: /api, /api/v1, /<word>, /api/v1/<word>."""
    return 1 <= len(head) <= 3 and "{}" not in head and sum(1 for s in head if not PREFIX.match(s)) <= 1


def by_suffix(method: str, path: str, routes: list[dict], admin: bool = False) -> int | None:
    """When no route matches whole, the one route (same method) whose path ends with the call's path after a plausible
    prefix; None when there is none or more than one. Needs at least two segments in the call."""
    cs = segments(path)
    if len(cs) < 2:
        return None
    hits = []
    for i, r in enumerate(routes):
        if str(r.get("m", "")).upper() not in (method.upper(), "ANY") or UNSURE in str(r.get("p", "")):
            continue
        rs = segments(str(r.get("p", "")))
        head = rs[:len(rs) - len(cs)]
        if "admin" in head and not admin and len(cs) < 3:  # a shop frontend doesn't reach the admin API by a short tail
            continue
        if len(rs) > len(cs) and _plausible_head(head) and _score(cs, rs[len(rs) - len(cs):]) is not None:
            hits.append(i)
    return hits[0] if len(hits) == 1 else None


def match_with_basis(method: str, path: str, routes: list[dict], admin: bool = False) -> tuple[int | None, str]:
    """(route index, "") for a whole-path match, (route index, "suffix") for a match by the path's tail, (None, "")."""
    i = match(method, path, routes)
    if i is not None:
        return i, ""
    j = by_suffix(method, path, routes, admin)
    return (j, "suffix") if j is not None else (None, "")


def match(method: str, path: str, routes: list[dict]) -> int | None:
    """Index of the route this call reaches, or None."""
    best: tuple[int, int] | None = None
    for i, r in enumerate(routes):
        rm = str(r.get("m", "")).upper()
        if rm not in (method.upper(), "ANY") or UNSURE in str(r.get("p", "")):
            continue
        s = score(path, str(r.get("p", "")))
        if s is None:
            continue
        s = s * 2 + (1 if rm == method.upper() else 0)
        if best is None or s > best[0]:
            best = (s, i)
    return best[1] if best else None
