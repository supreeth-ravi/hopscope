"""Go: routes registered with gin/echo/chi-style .GET("/x") calls and net/http HandleFunc."""
from __future__ import annotations

import re
from pathlib import Path

from langs.common import line_of, shared_externals

NAME = "go"
EXTENSIONS = (".go",)
SERVICE_MARKERS = ("go.mod",)

GO_ROUTE = re.compile(r"\.(GET|POST|PUT|PATCH|DELETE|HandleFunc|Handle)\(\s*\"([^\"]+)\"")


def routes(path: Path, text: str, ctx=None) -> list[dict]:
    return [{"m": ("ANY" if m.group(1).startswith("Handle") else m.group(1)), "p": m.group(2), "t": "", "d": "", "l": line_of(text, m.start())}
            for m in GO_ROUTE.finditer(text) if m.group(2).startswith("/")]


def imports(path: Path, text: str, ctx) -> list[int]:
    return []  # Go packages are folders; links between them are not resolved yet


def externals(text: str, ctx=None) -> set[str]:
    return shared_externals(text)
