# skills/hopscope/scripts/iac_compose.py
"""docker compose → design rows. Services that build from a folder are our code; database, cache
and search images become data rows; published ports become entry routes."""
from __future__ import annotations

import posixpath
import re
from typing import Any

import yaml

import iac_common as c

FILE = re.compile(r"(^|/)(docker-)?compose[^/]*\.ya?ml$", re.I)


def _env(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(k): "" if v is None else str(v) for k, v in raw.items()}
    out = {}
    for item in raw or []:
        k, _, v = str(item).partition("=")
        out[k] = v
    return out


def _build_dir(raw: Any, rel: str) -> str | None:
    """The code folder a service builds from, relative to the code base; None for remote, absolute,
    variable or out-of-repo contexts."""
    if raw is None:
        return None
    ctx = raw if isinstance(raw, str) else (raw.get("context") if isinstance(raw, dict) else None) or "."
    ctx = str(ctx)
    if ctx.startswith("/") or "://" in ctx or "$" in ctx:
        return None
    path = posixpath.normpath(posixpath.join(posixpath.dirname(rel), ctx))
    return None if path == ".." or path.startswith("../") else path


def _port(p: Any) -> int | None:
    """The port a service is reached on: the published one when there is one, else the container's."""
    if isinstance(p, dict):
        spec = str(p.get("published") or p.get("target") or "")
    else:
        parts = str(p).split("/")[0].split(":")
        spec = parts[-2] if len(parts) >= 3 else parts[0]
    m = re.match(r"\d+", spec)
    return int(m.group(0)) if m else None


def _load(text: str) -> dict | None:
    try:
        for doc in yaml.safe_load_all(text):
            if isinstance(doc, dict) and isinstance(doc.get("services"), dict):
                return doc["services"]
    except yaml.YAMLError:
        pass
    return None


def read(text: str, rel: str) -> dict:
    services = _load(text)
    part: dict = {"ecs": [], "rds": [], "cache": [], "search": [], "lb": []}
    if services is None:
        return part
    cluster = posixpath.basename(posixpath.dirname(rel)) or "compose"
    bare = cluster == "compose"
    entry = c.lb_row("compose" if bare else f"{cluster} (compose)", source="compose")
    internal = c.lb_row("compose (internal)" if bare else f"{cluster} (compose, internal)", internal=True, source="compose")  # expose: reachable by other services only
    known = {str(n): {int(x) for p in [*(sv.get("ports") or []), *(sv.get("expose") or [])] for x in re.findall(r"\d+", str(p))}
             for n, sv in services.items() if isinstance(sv, dict)} | {str(n): set() for n, sv in services.items() if not isinstance(sv, dict)}
    for name, s in services.items():
        s = s if isinstance(s, dict) else {}
        image, build = str(s.get("image") or ""), _build_dir(s.get("build"), rel)
        kind = c.infra_kind(image) if build is None else None
        if kind == "rds":
            part["rds"].append({**c.rds_row(name, c.image_engine(image), endpoint=name), "src": rel})
            continue
        if kind == "cache":
            part["cache"].append({**c.cache_row(name, c.image_engine(image)), "src": rel})
            continue
        if kind == "search":
            part["search"].append({"n": name, "src": rel})
            continue
        part["ecs"].append(c.ecs_row(cluster, name, desired=c.as_int((s.get("deploy") or {}).get("replicas") if isinstance(s.get("deploy"), dict) else None, 1),
                                     images=[image] if image else [], env_vars=_env(s.get("environment")), build=build, build_from=rel, source="compose",
                                     known_hosts=known))
        for lb, key in ((entry, "ports"), (internal, "expose")):
            for p in s.get(key) or []:
                port = _port(p)
                if port:
                    lb["routes"].append(c.route(f"port {port}", name, [f"{cluster}/{name}"], port=port))
    part["lb"].extend(lb for lb in (entry, internal) if lb["routes"])
    return part
