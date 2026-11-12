"""Operations an OpenAPI spec in a service declares ({operationId: (method, path, summary)}), for controllers that
implement interfaces generated from the spec at build time (the interfaces with the route annotations are then not in
the repository). Reads JSON, and the plain block-style YAML these specs use, with the standard library only."""
from __future__ import annotations

import json
import re
from functools import lru_cache

from langs.common import contained_glob

VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}
SPEC_GLOBS = ("src/main/resources/*.y*ml", "src/main/resources/*.json", "src/main/resources/*/*.y*ml", "*.y*ml", "api/*.y*ml")
KEY = re.compile(r"^(\s*)(['\"]?)([^'\":#]+?)\2\s*:\s*(.*?)\s*$")


def _from_json(text: str) -> dict[str, tuple[str, str, str]]:
    try:
        spec = json.loads(text)
    except ValueError:
        return {}
    out = {}
    for path, ops in (spec.get("paths") or {}).items() if isinstance(spec, dict) else []:
        for verb, op in (ops or {}).items() if isinstance(ops, dict) else []:
            if verb in VERBS and isinstance(op, dict) and op.get("operationId"):
                out[str(op["operationId"])] = (verb.upper(), str(path), str(op.get("summary") or "")[:140])
    return out


def _from_yaml(text: str) -> dict[str, tuple[str, str, str]]:
    out: dict[str, tuple[str, str, str]] = {}
    in_paths, path_indent, path, verb = False, -1, "", ""
    summary = ""
    for line in text.splitlines():
        m = KEY.match(line)
        if not m or line.lstrip().startswith("#"):
            continue
        indent, key, value = len(m.group(1)), m.group(3).strip(), m.group(4).strip("'\" ")
        if indent == 0:
            in_paths, path, verb = key == "paths", "", ""
            continue
        if not in_paths:
            continue
        if path_indent < 0:
            path_indent = indent
        if indent == path_indent:
            path, verb = key, ""
        elif path and key in VERBS and indent > path_indent:
            verb, summary = key, ""
        elif verb and key == "summary":
            summary = value
        elif verb and key == "operationId" and value:
            out[value] = (verb.upper(), path, summary[:140])
    return out


@lru_cache(maxsize=32)
def operations(root: str) -> dict[str, tuple[str, str, str]]:
    found: dict[str, tuple[str, str, str]] = {}
    for p in contained_glob(root, SPEC_GLOBS, 10):
        try:
            if p.stat().st_size > 2_000_000:
                continue
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        if not re.search(r"^\s*[\"']?(openapi|swagger)[\"']?\s*:", text, re.M):
            continue
        ops = _from_json(text) if p.suffix == ".json" else _from_yaml(text)
        for k, v in ops.items():
            found.setdefault(k, v)
    return found
