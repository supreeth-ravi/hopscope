"""Go: routes registered with gin/echo/chi-style .GET("/x") calls and net/http HandleFunc."""
from __future__ import annotations

import re
from pathlib import Path

from langs.common import line_of, shared_externals

NAME = "go"
EXTENSIONS = (".go",)
SERVICE_MARKERS = ("go.mod",)

GO_ROUTE = re.compile(r"\.(GET|POST|PUT|PATCH|DELETE)\(\s*\"([^\"]+)\"")
# net/http and gorilla/mux: HandleFunc("/x", h) or HandleFunc(prefix + "/x", h), optionally followed by .Methods(...)
HANDLE = re.compile(r"\.(?:HandleFunc|Handle)\(\s*(?:([A-Za-z_]\w*)\s*\+\s*)?\"([^\"]+)\"[^\n]*?\)(?:\s*\.Methods\(([^)]*)\))?")
METHOD = re.compile(r"http\.Method(Get|Post|Put|Patch|Delete|Head|Options)|\"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\"")
UNSURE = "{?}"  # a prefix the scanner can't resolve: such routes are never matched to frontend calls


def _prefix(text: str, var: str) -> str:
    """The value of a path prefix variable when every assignment in the file is the same string literal, else {?}."""
    rhs = re.findall(rf"(?:\b(?:const|var)\s+)?\b{re.escape(var)}\b(?:\s+string)?\s*:?=\s*([^\n;]+)", text)
    values = {m.group(1) for r in rhs if (m := re.fullmatch(r'\s*"([^"]*)"\s*', r))}
    return values.pop() if len(values) == 1 and len(values) == len(set(rhs)) else UNSURE


def _methods(args: str | None) -> list[str]:
    found = [(a or b).upper() for a, b in METHOD.findall(args or "")]
    found = [m for m in dict.fromkeys(found) if not (m == "HEAD" and "GET" in found)]  # HEAD comes with GET
    return found or ["ANY"]


def routes(path: Path, text: str, ctx=None) -> list[dict]:
    out = [(m.start(), m.group(1), m.group(2)) for m in GO_ROUTE.finditer(text) if m.group(2).startswith("/")]
    for m in HANDLE.finditer(text):
        if m.group(2).startswith("/"):
            full = (_prefix(text, m.group(1)) if m.group(1) else "") + m.group(2)
            out += [(m.start(), verb, full) for verb in _methods(m.group(3))]
    return [{"m": verb, "p": p, "t": "", "d": "", "l": line_of(text, pos)} for pos, verb, p in sorted(out, key=lambda x: x[0])]


def imports(path: Path, text: str, ctx) -> list[int]:
    return []  # Go packages are folders; links between them are not resolved yet


def externals(text: str, ctx=None) -> set[str]:
    return shared_externals(text)
