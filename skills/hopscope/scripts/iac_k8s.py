# skills/hopscope/scripts/iac_k8s.py
"""Kubernetes in design mode: plain manifests, Kustomize bases and overlays (iac_kustomize) and Helm charts
(iac_helm) under the repo, mapped to aws.json rows by k8s_map. Images are tied to the folder that builds them:
exactly from skaffold.yaml artifacts, then compose services with a build folder, then a Dockerfile folder named like
the image's repository.

The same object (namespace, kind, name) found in several places is read once, in this order: pre-rendered output
(rendered/ or manifests/ folders), Kustomize overlays, other plain manifests, Helm templates."""
from __future__ import annotations

import posixpath
import re
from pathlib import Path

import iac_common as c
import iac_compose
import iac_helm
import iac_kustomize
import k8s_files as kf
import k8s_map

MAX_BYTES = kf.MAX_BYTES
SKAFFOLD = re.compile(r"(^|/)skaffold\.ya?ml$", re.I)
DOCKERFILE = re.compile(r"(^|/)(Dockerfile|[\w.-]+\.Dockerfile)$")
Obj = tuple[dict, str]


def _builds(root: Path, files: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """(image or repository name -> code folder, image key -> the file that said so)."""
    exact: dict[str, str] = {}
    origin: dict[str, str] = {}
    compose: dict[str, str] = {}
    for rel in files:
        if SKAFFOLD.search(rel):
            try:
                configs = kf.docs(kf.read(root, rel))[0]
            except kf.Unreadable:
                continue
            for cfg in configs:
                for art in ((cfg.get("build") or {}).get("artifacts") or []) if isinstance(cfg.get("build"), dict) else []:
                    if isinstance(art, dict) and isinstance(art.get("image"), str):
                        ctx = kf.join_inside(posixpath.dirname(rel), str(art.get("context") or "."))
                        if ctx is not None:
                            for key in (art["image"], c.image_engine(art["image"])):
                                exact.setdefault(key, ctx)
                                origin.setdefault(key, rel)
        elif iac_compose.FILE.search(rel):
            try:
                services = iac_compose._load(kf.read(root, rel)) or {}
            except (kf.Unreadable, RecursionError, MemoryError):
                continue  # iac_scan reports the compose file itself
            for name, s in services.items():
                build = iac_compose._build_dir(s.get("build"), rel) if isinstance(s, dict) else None
                if build:
                    image = str(s.get("image") or "")
                    for key in ([image, c.image_engine(image)] if image else []) + [str(name)]:
                        compose.setdefault(key, build)
                        origin.setdefault(key, rel)
    by_folder: dict[str, list[str]] = {}
    for rel in files:
        if DOCKERFILE.search(rel):
            folder = posixpath.dirname(rel)
            by_folder.setdefault(posixpath.basename(folder) or ".", []).append(rel)
    docker = {name: posixpath.dirname(paths[0]) for name, paths in by_folder.items() if len({posixpath.dirname(x) for x in paths}) == 1 and name != "."}
    for name, found in by_folder.items():
        if name in docker:
            origin.setdefault(name, found[0])
    return {**docker, **compose, **exact}, origin


def _plain(root: Path, rel: str, skipped: list[dict]) -> list[Obj]:
    try:
        text = kf.read(root, rel)
    except kf.Unreadable as e:
        skipped.append({"file": rel, "error": str(e)})
        return []
    if "kind:" not in text or "apiVersion:" not in text:
        return []
    found, bad = kf.docs(text)
    objs = [(o, rel) for o in found if o.get("kind") in k8s_map.KINDS]
    if bad and (objs or re.search(r"(?m)^kind:\s*(" + "|".join(k8s_map.KINDS) + r")\s*$", text)):
        skipped.append({"file": rel, "error": "a YAML document could not be read"})
    return objs


def _kustomize_roots(root: Path, files: list[str], skipped: list[dict]) -> tuple[list[tuple[str, list[Obj]]], set[str]]:
    folders = sorted({posixpath.dirname(f) for f in files if posixpath.basename(f) in iac_kustomize.NAMES})
    referenced: set[str] = set()
    for d in folders:
        try:
            k = iac_kustomize.load(root, iac_kustomize.kustomization_file(root, d) or "")
        except kf.Unreadable:
            continue
        referenced |= {r for ref in iac_kustomize.references(k) if (r := kf.join_inside(d, ref)) is not None and r != d}
    used: set[str] = set()
    out = []
    for d in [x for x in folders if x not in referenced] + [x for x in folders if x in referenced]:
        kfile = iac_kustomize.kustomization_file(root, d)
        if d in referenced and kfile in used:
            continue  # a base: read through the overlays that use it
        objs = iac_kustomize.render(root, d, used, skipped)
        out.append((kfile or d, objs))
    return out, used


def _charts(files: list[str]) -> list[str]:
    return sorted({posixpath.dirname(f) for f in files if posixpath.basename(f) == "Chart.yaml"})


def _in_chart(rel: str, charts: list[str]) -> bool:
    return any(rel.startswith(f"{d}/templates/" if d else "templates/") or rel.startswith(f"{d}/charts/" if d else "charts/") for d in charts)


def _rendered(rel: str) -> bool:
    return any(seg in iac_helm.RENDERED for seg in rel.split("/")[:-1])


def collect(root: Path, files: list[str], skipped: list[dict]) -> tuple[list[Obj], dict[int, str]]:
    """Every object, best source first, and the kustomization file each Kustomize object came through (by object id)."""
    charts = _charts(files)
    yamls = [f for f in files if kf.YAML.search(f) and not SKAFFOLD.search(f) and not iac_compose.FILE.search(f)][:kf.MAX_FILES]
    roots, used = _kustomize_roots(root, yamls, skipped)
    plain = [f for f in yamls if f not in used and posixpath.basename(f) not in iac_kustomize.NAMES and not _in_chart(f, charts)
             and posixpath.basename(f) not in ("Chart.yaml", "values.yaml")]
    ordered: list[Obj] = []
    for rel in sorted(plain, key=lambda f: not _rendered(f)):
        if _rendered(rel):
            ordered += _plain(root, rel, skipped)
    kfiles: dict[int, str] = {}
    for kfile, objs in roots:
        ordered += objs
        kfiles.update((id(o), kfile) for o, _ in objs)
    for rel in plain:
        if not _rendered(rel):
            ordered += _plain(root, rel, skipped)
    for d in charts:
        if not iac_helm.pre_rendered(root, d, files):
            ordered += iac_helm.render(root, d, files, skipped)
    return ordered, kfiles


def dedupe(objs: list[Obj]) -> list[Obj]:
    seen: set[tuple[str, str, str]] = set()
    out = []
    for o, src in objs:
        key = (k8s_map.ns_of(o), str(o.get("kind")), k8s_map.name_of(o))
        if key[2] and key not in seen:
            seen.add(key)
            out.append((o, src))
    return out


def scan(base: Path) -> tuple[dict, list[str], list[dict]]:
    """(rows, the files they came from, files skipped with the reason) for the Kubernetes files under base."""
    root = base.resolve()
    files = kf.walk(root)
    skipped: list[dict] = []
    found, kfiles = collect(root, files, skipped)
    objs = [(o, src) for o, src in dedupe(found) if o.get("kind") in k8s_map.KINDS]
    if not objs:
        return {"ecs": [], "lb": []}, [], skipped
    builds, origin = _builds(root, files)
    part = k8s_map.map_objects(objs, builds)
    sources = {src for _, src in objs} | {kfiles[id(o)] for o, _ in objs if id(o) in kfiles}
    for x in part["ecs"]:
        key = next((k for k in x["images"] if k in origin), None)
        if key and x.get("build"):  # the linker's evidence names the file that ties the image to its folder first
            x["buildFrom"] = f'{origin[key]}, {x["buildFrom"]}'
            if not DOCKERFILE.search(origin[key]):
                sources.add(origin[key])
    return part, sorted(sources), skipped
