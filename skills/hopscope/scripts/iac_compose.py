# skills/hopscope/scripts/iac_compose.py
"""docker compose → design rows. Services that build from a folder are our code; database, cache
and search images become data rows; published ports become entry routes."""
from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any

import yaml

import iac_common as c

FILE = re.compile(r"(^|/)(docker-)?compose[^/]*\.ya?ml$", re.I)


def _env(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {c.scalar(k): c.scalar(v) for k, v in raw.items() if c.scalar(k)}
    out = {}
    for item in raw if isinstance(raw, list) else []:
        k, _, v = c.scalar(item).partition("=")
        out[k] = v
    return out


ENV_LINE = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(.*)")
ENV_FILE_LINES = 500
ENV_FILE_BYTES = 200_000


def _env_value(raw: str) -> str:
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return re.split(r"\s+#", v, maxsplit=1)[0].strip()


def _env_file_paths(raw: Any) -> list[str]:
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for it in items:
        path = it.get("path") if isinstance(it, dict) else it
        if isinstance(path, str) and path:
            out.append(path)
    return out


def _env_files(raw: Any, rel: str, base: Path | None) -> dict[str, str]:
    """KEY=VALUE lines from a service's env_file entries: only files inside the repo, missing ones skipped,
    the first 500 lines of each. Values are returned to the caller's allowlist, never logged."""
    if raw is None or base is None:
        return {}
    root = base.resolve()
    out: dict[str, str] = {}
    for path in _env_file_paths(raw):
        if _unsafe(path):
            continue
        local = posixpath.normpath(posixpath.join(posixpath.dirname(rel), path))
        if local == ".." or local.startswith("../"):
            continue
        f = base / local
        try:
            if f.is_symlink() or not f.is_file() or not f.resolve().is_relative_to(root) or f.stat().st_size > ENV_FILE_BYTES:
                continue
            lines = f.read_text(errors="ignore").splitlines()[:ENV_FILE_LINES]
        except OSError:
            continue
        for line in lines:
            m = ENV_LINE.fullmatch(line.strip())
            if m and not line.lstrip().startswith("#"):
                out[m.group(1)] = _env_value(m.group(2))
    return out


def _unsafe(path: str) -> bool:
    return path.startswith("/") or "://" in path or "$" in path


def _below(path: str, folder: str) -> bool:
    return path != folder and not path.startswith("../") and path != ".." and (folder == "." or path.startswith(folder + "/"))


def _build_dir(raw: Any, rel: str) -> str | None:
    """The code folder a service builds from, relative to the code base; None for remote, absolute,
    variable or out-of-repo contexts. A dockerfile in a folder below the context (context: ../..,
    dockerfile: admin/Dockerfile) names the code more exactly than the context does."""
    if raw is None:
        return None
    ctx = raw if isinstance(raw, str) else (raw.get("context") if isinstance(raw, dict) else None) or "."
    ctx = c.scalar(ctx)
    if _unsafe(ctx):
        return None
    path = posixpath.normpath(posixpath.join(posixpath.dirname(rel), ctx))
    if path == ".." or path.startswith("../"):
        return None
    dockerfile = c.scalar(raw.get("dockerfile") or "") if isinstance(raw, dict) else ""
    if dockerfile and not _unsafe(dockerfile):
        folder = posixpath.normpath(posixpath.join(path, posixpath.dirname(dockerfile)))
        if _below(folder, path):
            return folder
    return path


def _port(p: Any) -> int | None:
    """The port a service is reached on: the published one when there is one, else the container's."""
    if isinstance(p, dict):
        spec = c.scalar(p.get("published") or p.get("target") or "")
    else:
        parts = c.scalar(p).split("/")[0].split(":")
        spec = parts[-2] if len(parts) >= 3 else parts[0]
    m = re.match(r"\d+", spec)
    return int(m.group(0)) if m else None


def _items(v: Any) -> list:
    return v[:200] if isinstance(v, list) else []


def _load(text: str) -> dict | None:
    try:
        for doc in yaml.safe_load_all(text):
            if isinstance(doc, dict) and isinstance(doc.get("services"), dict):
                return doc["services"]
    except yaml.YAMLError:
        pass
    return None


VARIANT = re.compile(r"^(?:docker-)?compose[._-]?(.*?)\.ya?ml$", re.I)


def cluster_of(rel: str) -> str:
    """The folder name, plus the variant of a docker-compose.<variant>.yml: deploy/docker-compose.staging.yml
    -> deploy-staging. Files at the root are "compose"."""
    folder = posixpath.basename(posixpath.dirname(rel)) or "compose"
    m = VARIANT.match(posixpath.basename(rel))
    variant = re.sub(r"[^A-Za-z0-9_-]+", "-", m.group(1)).strip("-_") if m else ""
    return f"{folder}-{variant}" if variant else folder


def read(text: str, rel: str, base: Path | None = None) -> dict:
    """Rows from one compose file at `rel`; with the repo `base`, env_file entries are read too."""
    services = _load(text)
    part: dict = {"ecs": [], "rds": [], "cache": [], "search": [], "lb": []}
    if services is None:
        return part
    cluster = cluster_of(rel)
    bare = not posixpath.dirname(rel)  # at the root the cluster name already says compose
    entry = c.lb_row(cluster if bare else f"{cluster} (compose)", source="compose")
    internal = c.lb_row(f"{cluster} (internal)" if bare else f"{cluster} (compose, internal)", internal=True, source="compose")  # expose: reachable by other services only
    known = {str(n): {int(x) for p in [*_items(sv.get("ports")), *_items(sv.get("expose"))] for x in re.findall(r"\d+", c.scalar(p))}
             for n, sv in services.items() if isinstance(sv, dict)} | {str(n): set() for n, sv in services.items() if not isinstance(sv, dict)}
    for name, s in services.items():
        s = s if isinstance(s, dict) else {}
        image, build = c.scalar(s.get("image") or ""), _build_dir(s.get("build"), rel)
        kind = c.infra_kind(image) if build is None else None
        if kind == "rds":
            part["rds"].append({**c.rds_row(name, c.image_engine(image), endpoint=name), "env": c.env_of(f"{cluster}/{name}"), "cluster": cluster, "src": rel})
            continue
        if kind == "cache":
            part["cache"].append({**c.cache_row(name, c.image_engine(image)), "endpoint": name, "cluster": cluster, "src": rel})
            continue
        if kind == "search":
            part["search"].append({"n": name, "endpoint": name, "cluster": cluster, "src": rel})
            continue
        part["ecs"].append(c.ecs_row(cluster, name, desired=c.as_int((s.get("deploy") or {}).get("replicas") if isinstance(s.get("deploy"), dict) else None, 1),
                                     images=[image] if image else [], env_vars={**_env_files(s.get("env_file"), rel, base), **_env(s.get("environment"))}, build=build, build_from=rel, source="compose",
                                     known_hosts=known))
        for lb, key in ((entry, "ports"), (internal, "expose")):
            for p in _items(s.get(key)):
                port = _port(p)
                if port:
                    lb["routes"].append(c.route(f"port {port}", name, [f"{cluster}/{name}"], port=port))
    part["lb"].extend(lb for lb in (entry, internal) if lb["routes"])
    return part
