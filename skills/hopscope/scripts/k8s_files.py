# skills/hopscope/scripts/k8s_files.py
"""Contained file access for the Kubernetes readers (iac_k8s, iac_kustomize, iac_helm): only regular files inside the
repo, never through a symlink, at most MAX_BYTES each; YAML through yaml.safe_load_all, one document at a time."""
from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path
from typing import Any

import yaml

SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".terraform", ".aws-sam", "cdk.out", "floorplan", ".hopscope", "vendor"}
MAX_BYTES = 1_000_000
MAX_FILES = 5000
MAX_DOCS = 500
YAML = re.compile(r"\.ya?ml$", re.I)
REMOTE = re.compile(r"://|^(github\.com|gitlab\.com|bitbucket\.org)/|^git@|\?ref=", re.I)


class NoAliasLoader(yaml.SafeLoader):
    """safe_load without aliases: Kubernetes, Kustomize and Helm files have no use for them, and an alias bomb
    ("billion laughs") would expand into gigabytes the moment anything reads it."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not read in Kubernetes files")
        return super().compose_node(parent, index)


def load_yaml(text: str) -> Any:
    """One YAML document; Unreadable for aliases, bad syntax or nesting too deep to parse."""
    try:
        return yaml.load(text, Loader=NoAliasLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as e:
        raise Unreadable("YAML aliases are not read" if "aliases" in str(e) else "not valid YAML") from e
    except (RecursionError, MemoryError) as e:
        raise Unreadable("nested too deeply to read") from e


class Unreadable(Exception):
    """A file a reader was pointed at but may not or cannot read; the message says why, without its content."""


def shown(ref: str) -> str:
    """A reference as a skipped-file message may show it: a local absolute or home path is reduced to its file name, so
    the report never carries someone's directory layout."""
    if ref.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", ref):
        return posixpath.basename(ref.replace("\\", "/").rstrip("/"))[:80] or "a local path"
    return ref[:80]


def join_inside(folder: str, ref: str) -> str | None:
    """folder/ref as a repo-relative path, or None when it is absolute, remote, a variable or leaves the repo."""
    if not ref or ref.startswith(("/", "~")) or "$" in ref or REMOTE.search(ref) or "\\" in ref:
        return None
    rel = posixpath.normpath(posixpath.join(folder, ref))
    return None if rel == ".." or rel.startswith("../") else rel


def _check(root: Path, rel: str) -> Path:
    p = root / rel
    if p.is_symlink() or any(q.is_symlink() for q in p.parents if q != root and root in q.parents):
        raise Unreadable("symlink not followed")
    if not p.resolve().is_relative_to(root):
        raise Unreadable("outside the repo")
    return p


def is_dir(root: Path, rel: str) -> bool:
    try:
        return _check(root, rel).is_dir()
    except (Unreadable, OSError):
        return False


def read(root: Path, rel: str) -> str:
    """The text of a regular file inside root (root must be resolved)."""
    try:
        p = _check(root, rel)
        if not p.is_file():
            raise Unreadable("not a file")
        if p.stat().st_size > MAX_BYTES:
            raise Unreadable("too large")
        return p.read_text(errors="ignore")
    except OSError as e:
        raise Unreadable(type(e).__name__) from e


def docs(text: str) -> tuple[list[dict], int]:
    """The mapping documents of a multi-document YAML text, and how many documents could not be parsed (bad syntax,
    aliases, nesting too deep). Each document is parsed on its own so one broken document doesn't lose the rest."""
    out: list[dict] = []
    bad = 0
    for chunk in re.split(r"(?m)^---[ \t]*(?:#.*)?$", text)[:MAX_DOCS]:
        if not chunk.strip():
            continue
        try:
            d: Any = load_yaml(chunk)
        except Unreadable:
            bad += 1
            continue
        if isinstance(d, dict):
            out.append(d)
            if d.get("kind") == "List" and isinstance(d.get("items"), list):
                out.extend(x for x in d["items"] if isinstance(x, dict))
    return out, bad


def walk(root: Path) -> list[str]:
    """Repo-relative paths of the regular files under root, skipping vendored and build folders and symlinks."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP and not d.startswith(".") or d in (".k8s", ".kube"))
        for f in sorted(filenames):
            p = Path(dirpath) / f
            if p.is_symlink():
                continue
            out.append(p.relative_to(root).as_posix())
            if len(out) >= MAX_FILES * 4:
                return out
    return out
