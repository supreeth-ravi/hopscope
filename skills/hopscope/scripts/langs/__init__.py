"""Language plugins for the code scanner (code_scan.py is the driver).

Each language is one module in this package. The contract:

Required
  NAME: str                         short id, e.g. "python"
  EXTENSIONS: tuple[str, ...]       file suffixes the module reads, e.g. (".py",)
  SERVICE_MARKERS: tuple[str, ...]  file names that mark a service folder; "*" globs allowed ("*.csproj")
  routes(path, text, ctx) -> list[dict]
                                    API routes defined in the file: {"m": method, "p": path, "t": title, "d": description,
                                    "l": line}, plus "f" (a file index) when the handler lives in another file (a Rails
                                    or Laravel route file naming a controller action; "l" is then the handler's line).
                                    Methods outside code_scan.METHODS are dropped by the driver. ctx (see imports) may be
                                    None.
  imports(path, text, ctx) -> list[int]
                                    indices of the scanned files this file imports (ctx.index maps a resolved path to its
                                    index; ctx.root is the file's service folder, ctx.repo its repository)
  externals(text, ctx) -> set[str]  external systems the file talks to (keys of the console's EXTERNAL map, e.g.
                                    "postgres", "sqs", "kafka"); usually langs.common.shared_externals(text) plus
                                    language-specific SDK patterns

Optional
  TEST_FILE: re.Pattern             file names that are tests (skipped)
  BUILD_DIRS: dict[str, str]        build-output folder -> marker glob; the folder is skipped when its parent holds a
                                    matching file (e.g. {"obj": "*.csproj"})
  prepare(files) -> object          called once per scan with [(index, path, text)] of this language's files; the result
                                    is ctx.state for every later call (e.g. a package -> files index)
  mounts(path, text, ctx) -> list[tuple[int, str]]
                                    files this one mounts under a prefix (FastAPI include_router), as (file index, prefix)
  client_calls(path, text, ctx) -> list[tuple[int, str, str]]
                                    HTTP calls a frontend file makes, as (line, method, path with {params}); the driver
                                    runs it on frontend files only and matches each call to a route (langs/clientmatch.py)
  flows(data, base, route_index) -> list[list] | None
                                    the call sequence of one route's handler, in flows.json's step shape (see
                                    call_flow.py); None when the language has no call-flow support

Adding a language: write the module, add it to LANGUAGES below, add fixture tests (see docs/development.md).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from langs import csharp, go, java, js, php, python, ruby, rust

LANGUAGES: tuple[ModuleType, ...] = (python, js, go, java, csharp, ruby, php, rust)
GENERIC_MARKERS = ("Dockerfile",)


@dataclass
class Context:
    index: dict[str, int]
    root: Path
    repo: Path
    state: Any = None
    cache: dict = field(default_factory=dict)


def by_suffix() -> dict[str, ModuleType]:
    return {ext: lang for lang in LANGUAGES for ext in lang.EXTENSIONS}


def service_markers() -> tuple[str, ...]:
    out = list(GENERIC_MARKERS)
    for lang in LANGUAGES:
        out += [m for m in lang.SERVICE_MARKERS if m not in out]
    return tuple(out)


def build_dirs() -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for lang in LANGUAGES:
        for d, marker in getattr(lang, "BUILD_DIRS", {}).items():
            out[d] = out.get(d, ()) + (marker,)
    return out


def flows_for(data: dict, base: Path, route_index: int) -> list[list] | None:
    """The call sequence of a route whose handler is in a language with a flows() hook, or None."""
    files = data.get("files", [])
    r = data["routes"][route_index]
    path = files[r["f"]]["p"] if 0 <= r.get("f", -1) < len(files) else ""
    lang = by_suffix().get(Path(path).suffix)
    if lang is None or not hasattr(lang, "flows"):
        return None
    try:
        return lang.flows(data, base, route_index)
    except (OSError, ValueError, RecursionError, IndexError) as e:
        print(f"  call flow skipped for {path}: {e}", file=sys.stderr)
        return None
