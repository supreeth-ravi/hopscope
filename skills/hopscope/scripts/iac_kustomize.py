# skills/hopscope/scripts/iac_kustomize.py
"""Kustomize without the kustomize binary: what a kustomization.yaml literally says. resources/bases/components inside
the repo (remote bases are listed as skipped, never fetched), configMapGenerator literals, strategic-merge patches by
kind and name, images, replicas, then namePrefix/nameSuffix (with the references that follow them) and namespace."""
from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Any

import k8s_files as kf
import k8s_map

NAMES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
MAX_DEPTH = 12
SCALABLE = ("Deployment", "StatefulSet", "ReplicaSet", "ReplicationController")  # what kustomize's replicas field changes
Obj = tuple[dict, str]


def kustomization_file(root: Path, folder: str) -> str | None:
    for n in NAMES:
        rel = posixpath.join(folder, n) if folder not in ("", ".") else n
        if (root / rel).is_file() and not (root / rel).is_symlink():
            return rel
    return None


def load(root: Path, kfile: str) -> dict:
    k = kf.load_yaml(kf.read(root, kfile))
    return k if isinstance(k, dict) else {}


def references(k: dict) -> list[str]:
    return [r for key in ("resources", "bases", "components") for r in (k.get(key) or []) if isinstance(r, str)] if isinstance(k, dict) else []


def merge(base: Any, patch: Any) -> Any:
    """A strategic-merge patch, simplified: maps merge key by key, lists of named maps merge by name, other lists replace."""
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            if k == "$patch":
                continue
            out[k] = merge(base.get(k), v) if k in base else v
        return out
    if isinstance(base, list) and isinstance(patch, list) and all(isinstance(x, dict) and "name" in x for x in base + patch):
        merged = list(base)
        for p in patch:
            i = next((j for j, b in enumerate(merged) if b["name"] == p["name"]), None)
            if i is None:
                merged.append(p)
            else:
                merged[i] = merge(merged[i], p)
        return merged
    return patch


def _name(o: dict) -> str:
    return str((o.get("metadata") or {}).get("name") or "") if isinstance(o.get("metadata"), dict) else ""


def _patches(root: Path, folder: str, k: dict, skipped: list[dict], kfile: str, used: set[str]) -> list[dict]:
    out: list[dict] = []
    entries: list[dict[str, Any]] = [{"path": p} for p in k.get("patchesStrategicMerge") or [] if isinstance(p, str)] + [p for p in k.get("patches") or [] if isinstance(p, dict)]
    for e in entries:
        text = e.get("patch") if isinstance(e.get("patch"), str) else None
        if text is None and isinstance(e.get("path"), str):
            rel = kf.join_inside(folder, e["path"])
            try:
                if rel is None:
                    raise kf.Unreadable("outside the repo")
                text = kf.read(root, rel)
                used.add(rel)
            except kf.Unreadable as err:
                skipped.append({"file": kfile, "error": f"patch {kf.shown(str(e['path']))}: {err}"})
                continue
        target: dict[str, Any] = e["target"] if isinstance(e.get("target"), dict) else {}
        for d in kf.docs(text or "")[0]:
            if d.get("kind") or target:
                out.append({**d, "kind": d.get("kind") or target.get("kind"),
                            "metadata": {**(d.get("metadata") or {}), **({"name": target["name"]} if target.get("name") else {})}})
    return out


def _generated(k: dict, kfile: str) -> list[Obj]:
    out: list[Obj] = []
    for g in k.get("configMapGenerator") or []:
        if isinstance(g, dict) and isinstance(g.get("name"), str):
            data = dict(str(x).split("=", 1) for x in g.get("literals") or [] if "=" in str(x))
            out.append(({"kind": "ConfigMap", "metadata": {"name": g["name"]}, "data": data}, kfile))
    return out


def _set_image(ct: dict, images: list[dict]) -> None:
    img = str(ct.get("image") or "")
    bare = img.split("@")[0].rsplit(":", 1)[0] if ":" in img.rsplit("/", 1)[-1] else img.split("@")[0]
    for i in images:
        if isinstance(i, dict) and i.get("name") in (bare, bare.rsplit("/", 1)[-1]):
            tag = f'@{i["digest"]}' if i.get("digest") else f':{i["newTag"]}' if i.get("newTag") else ""
            ct["image"] = f'{i.get("newName") or bare}{tag}'


def _containers(o: dict) -> list[dict]:
    return k8s_map.containers(o)


def _rename_refs(o: dict, renamed: dict[str, str]) -> None:
    """References to renamed objects follow them: Ingress and HTTPRoute backends, HPA targets, ConfigMap references."""
    spec: dict[str, Any] = o["spec"] if isinstance(o.get("spec"), dict) else {}
    for rule in spec.get("rules") or []:
        for p in ((rule.get("http") or {}).get("paths") or []) if isinstance(rule, dict) else []:
            svc = ((p.get("backend") or {}).get("service") or {}) if isinstance(p, dict) else {}
            if isinstance(svc, dict) and svc.get("name") in renamed:
                svc["name"] = renamed[svc["name"]]
        for ref in (rule.get("backendRefs") or []) if isinstance(rule, dict) else []:
            if isinstance(ref, dict) and ref.get("name") in renamed:
                ref["name"] = renamed[ref["name"]]
    target = spec.get("scaleTargetRef")
    if isinstance(target, dict) and target.get("name") in renamed:
        target["name"] = renamed[target["name"]]
    for ct in _containers(o):
        refs = [e.get("configMapRef") for e in ct.get("envFrom") or [] if isinstance(e, dict)]
        refs += [(e.get("valueFrom") or {}).get("configMapKeyRef") for e in ct.get("env") or [] if isinstance(e, dict) and isinstance(e.get("valueFrom"), dict)]
        for r in refs:
            if isinstance(r, dict) and r.get("name") in renamed:
                r["name"] = renamed[r["name"]]


def transform(objs: list[Obj], k: dict, patches: list[dict]) -> list[Obj]:
    out = []
    for o, src in objs:
        for p in patches:
            if p.get("kind") == o.get("kind") and _name(p) == _name(o):
                o = merge(o, p)
        out.append((o, src))
    images = [i for i in k.get("images") or [] if isinstance(i, dict)]
    counts = {r["name"]: r.get("count") for r in k.get("replicas") or [] if isinstance(r, dict) and isinstance(r.get("name"), str)}
    prefix, suffix = str(k.get("namePrefix") or ""), str(k.get("nameSuffix") or "")
    renamed = {_name(o): f"{prefix}{_name(o)}{suffix}" for o, _ in out if (prefix or suffix) and _name(o)}
    ns = k.get("namespace") if isinstance(k.get("namespace"), str) else None
    for o, _ in out:
        for ct in _containers(o):
            _set_image(ct, images)
        if _name(o) in counts and o.get("kind") in SCALABLE and isinstance(o.get("spec"), dict):
            o["spec"]["replicas"] = counts[_name(o)]
        _rename_refs(o, renamed)
        meta = o.get("metadata")
        if not isinstance(meta, dict):
            meta = o["metadata"] = {}
        if _name(o) in renamed:
            meta["name"] = renamed[_name(o)]
        if ns and o.get("kind") not in ("IngressClass", "Namespace"):
            meta["namespace"] = ns
    return out


def render(root: Path, folder: str, used: set[str], skipped: list[dict], stack: tuple[str, ...] = ()) -> list[Obj]:
    """The objects a kustomization folder produces, with `used` collecting the files it read."""
    kfile = kustomization_file(root, folder)
    if kfile is None:
        return []
    if folder in stack or len(stack) > MAX_DEPTH:
        skipped.append({"file": kfile, "error": "kustomization cycle or nesting too deep"})
        return []
    try:
        k = load(root, kfile)
    except kf.Unreadable as e:
        skipped.append({"file": kfile, "error": str(e)})
        return []
    used.add(kfile)
    objs: list[Obj] = []
    for ref in references(k):
        rel = kf.join_inside(folder, ref)
        if rel is None:
            skipped.append({"file": kfile, "error": f"{'remote base not read' if kf.REMOTE.search(ref) else 'outside the repo'}: {kf.shown(ref)}"})
            continue
        if kf.is_dir(root, rel):
            objs += render(root, rel, used, skipped, (*stack, folder))
            continue
        try:
            found, bad = kf.docs(kf.read(root, rel))
        except kf.Unreadable as e:
            skipped.append({"file": kfile, "error": f"{kf.shown(ref)}: {e}"})
            continue
        if bad:
            skipped.append({"file": rel, "error": "a YAML document could not be read"})
        used.add(rel)
        objs += [(o, rel) for o in found]
    objs += _generated(k, kfile)
    return transform(objs, k, _patches(root, folder, k, skipped, kfile, used))
