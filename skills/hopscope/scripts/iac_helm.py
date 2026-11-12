# skills/hopscope/scripts/iac_helm.py
"""Helm charts, best effort, without the helm binary: Chart.yaml and values.yaml are read, and in each template only
simple expressions are filled in: {{ .Values.a.b }} (a plain value), {{ .Release.Name }}, {{ include "x.fullname" . }}
and {{ .Chart.Name }} (all the chart's name), {{ .Chart.Version }} and {{ .Chart.AppVersion }}. A line holding any other
template logic (if, range, toYaml, pipes) is left out, so both branches of an if are read and a block that only a
function could build is missing. A chart with a rendered/ or manifests/ folder next to it is not templated: that
pre-rendered output is read as plain manifests instead."""
from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any

import k8s_files as kf

EXPR = re.compile(r"\{\{-?\s*(.*?)\s*-?\}\}")
VALUE = re.compile(r"^\.Values((?:\.[A-Za-z_][\w-]*)+)$")
INCLUDE = re.compile(r'^include\s+"[\w.-]+\.fullname"\s+\.$')
RENDERED = ("rendered", "manifests")
Obj = tuple[dict, str]


def _yaml(root: Path, rel: str) -> Any:
    try:
        return kf.load_yaml(kf.read(root, rel))
    except kf.Unreadable:
        return None


def _scalar(v: Any) -> str | None:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (str, int, float)):
        return str(v)
    return None


def _value(values: dict, path: str) -> str | None:
    cur: Any = values
    for part in path.strip(".").split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return _scalar(cur)


def fill(line: str, chart: dict[str, str], values: dict) -> str | None:
    """The line with every expression filled in, or None when one of them is not simple."""
    out, last = [], 0
    for m in EXPR.finditer(line):
        e = m.group(1)
        v = VALUE.match(e)
        if v:
            val = _value(values, v.group(1))
        elif e in (".Release.Name", ".Chart.Name") or INCLUDE.match(e):
            val = chart["name"]
        elif e in (".Chart.Version", ".Chart.AppVersion"):
            val = chart.get(e.rsplit(".", 1)[1].lower()) or None
        else:
            val = None
        if val is None:
            return None
        out += [line[last:m.start()], val]
        last = m.end()
    return "".join(out) + line[last:]


def render_text(text: str, chart: dict[str, str], values: dict) -> str:
    return "\n".join(x for line in text.splitlines() if (x := fill(line, chart, values)) is not None)


def pre_rendered(root: Path, chart_dir: str, files: list[str]) -> bool:
    """A rendered/ or manifests/ folder in the chart or next to it holds YAML: read that instead of templating."""
    parent = posixpath.dirname(chart_dir)
    folders = {posixpath.join(d, r) if d else r for d in {chart_dir, parent} for r in RENDERED}
    return any(kf.YAML.search(f) and posixpath.dirname(f) in folders for f in files)


def render(root: Path, chart_dir: str, files: list[str], skipped: list[dict]) -> list[Obj]:
    meta = _yaml(root, posixpath.join(chart_dir, "Chart.yaml")) if chart_dir else _yaml(root, "Chart.yaml")
    meta = meta if isinstance(meta, dict) else {}
    chart = {"name": str(meta.get("name") or posixpath.basename(chart_dir) or "chart"),
             "version": _scalar(meta.get("version")) or "", "appversion": _scalar(meta.get("appVersion")) or ""}
    values = _yaml(root, posixpath.join(chart_dir, "values.yaml") if chart_dir else "values.yaml")
    values = values if isinstance(values, dict) else {}
    prefix = posixpath.join(chart_dir, "templates") + "/" if chart_dir else "templates/"
    out: list[Obj] = []
    for rel in files:
        if not rel.startswith(prefix) or not kf.YAML.search(rel) or posixpath.basename(rel).startswith("_"):
            continue
        try:
            found, bad = kf.docs(render_text(kf.read(root, rel), chart, values))
        except kf.Unreadable as e:
            skipped.append({"file": rel, "error": str(e)})
            continue
        if bad:
            skipped.append({"file": rel, "error": "part of the template could not be read without helm"})
        out += [(o, rel) for o in found]
    return out
