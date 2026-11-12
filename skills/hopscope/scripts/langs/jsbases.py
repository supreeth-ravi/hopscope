"""Base paths of HTTP clients a frontend defines once and calls many times: axios/ky instances
(`const statsApi = axios.create({ baseURL: `${API_URL}/api/v1/stats` })`) and fetch wrappers
(`function apiFetch(path) { return fetch(`${API_URL}/api/v1${path}`) }`), in the file itself or imported from a
relative module. Only the literal path part of a base is kept (/api/v1/stats): scheme, host and environment
variables are dropped. Used by jsclient.client_calls. Standard library only."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from langs.common import blanked, match_close, split_top

INSTANCE = re.compile(r"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:axios|ky)\.(?:create|extend)\s*\(")
WRAPPER = re.compile(r"(?:\bfunction\s+(\w+)\s*\(\s*(\w+)|\b(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(\s*(\w+)[^)]*\)\s*(?::\s*[^=;{]+)?=>)")
IMPORT = re.compile(r"\bimport\s+(?:(\w+)\s*,?\s*)?(?:\{([^}]*)\})?\s*from\s*['\"](\.{1,2}/[^'\"]+)['\"]")
DEFAULT = re.compile(r"\bexport\s+default\s+(\w+)\s*;?")
MARK = "\x01"
EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs")


def base_path(url: str) -> str:
    """URL text from jsclient._value -> its literal path prefix ('' when none)."""
    u = url.replace("\x00", "")
    u = re.sub(r"^(?:https?:)?//[^/]*", "", u)
    u = re.split(r"[?#]", u, maxsplit=1)[0]
    segs = [s for s in u.split("/") if s and not s.startswith("{")]
    return "/" + "/".join(segs) if segs else ""


def _object_value(obj: str, key: str) -> str:
    inner = obj.strip()
    if inner.startswith("{"):
        inner = inner[1:match_close(inner, 0)]
    for part in split_top(inner):
        m = re.match(r"\s*['\"]?" + key + r"['\"]?\s*:\s*(.+)$", part, re.S)
        if m:
            return m.group(1).strip()
    return ""


def local_bases(code: str, consts: dict[str, str]) -> tuple[dict[str, tuple[str, str]], list[tuple[int, int]]]:
    """{name: (kind, base path)} for instances and wrappers in this file, and the spans of wrapper bodies (their own
    fetch is not a call to record)."""
    from langs.jsclient import _value
    out: dict[str, tuple[str, str]] = {}
    spans: list[tuple[int, int]] = []
    for m in INSTANCE.finditer(code):
        close = match_close(code, m.end() - 1, "(", ")")
        expr = _object_value(code[m.end():close], "baseURL") or _object_value(code[m.end():close], "prefixUrl")
        out[m.group(1)] = ("instance", base_path(_value(expr, consts)) if expr else "")
    for m in WRAPPER.finditer(code):
        name, param = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        body = code[m.end():m.end() + 800]
        f = re.search(r"\bfetch\s*\(", body)
        if not f:
            continue
        open_ = m.end() + f.end() - 1
        args = split_top(code[open_ + 1:match_close(code, open_, "(", ")")])
        text = _value(args[0], {**consts, param: "'" + MARK + "'"}) if args else ""
        if MARK in text:
            out[name] = ("wrapper", base_path(text.split(MARK)[0]))
            spans.append((m.start(), m.end() + f.end()))
    return out, spans


def _resolve(path: Path, spec: str, index: dict[str, int]) -> Path | None:
    """A relative import resolved to a scanned file only (ctx.index): never a file outside the repos or a symlink."""
    base = (path.parent / spec).resolve()
    for cand in (base, *(base.with_suffix(e) for e in EXTS), *(base / f"index{e}" for e in EXTS)):
        if str(cand) in index and not cand.is_symlink():
            return cand
    return None


@lru_cache(maxsize=256)
def exported_bases(path: Path) -> dict[str, tuple[str, str]]:
    """Bases a module defines, by exported name ("default" for its default export)."""
    from langs.jsclient import _consts
    try:
        if path.stat().st_size > 400_000:
            return {}
        code = blanked(path.read_text(errors="ignore"))
    except OSError:
        return {}
    bases, _ = local_bases(code, _consts(code))
    d = DEFAULT.search(code)
    if d and d.group(1) in bases:
        bases = {**bases, "default": bases[d.group(1)]}
    return bases


def imported_bases(path: Path, code: str, index: dict[str, int]) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for m in IMPORT.finditer(code):
        target = _resolve(path, m.group(3), index)
        if target is None:
            continue
        theirs = exported_bases(target)
        if m.group(1) and "default" in theirs:
            out[m.group(1)] = theirs["default"]
        for item in (m.group(2) or "").split(","):
            parts = item.strip().split(" as ")
            if parts[0].strip() in theirs:
                out[parts[-1].strip()] = theirs[parts[0].strip()]
    return out


def join(base: str, url: str) -> str:
    """A relative call path under an instance's base: '/api/v1' + '/agents' -> '/api/v1/agents'."""
    if not base or re.match(r"^(?:https?:)?//", url) or url.startswith("\x00"):
        return url
    return base.rstrip("/") + "/" + url.lstrip("/")
