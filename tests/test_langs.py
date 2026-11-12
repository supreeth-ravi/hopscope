"""The language plugin contract (skills/hopscope/scripts/langs)."""
from __future__ import annotations

from pathlib import Path

import pytest

import code_scan
import langs


@pytest.mark.parametrize("lang", langs.LANGUAGES, ids=lambda m: m.NAME)
def test_every_language_meets_the_contract(lang) -> None:
    assert isinstance(lang.NAME, str) and lang.NAME
    assert lang.EXTENSIONS and all(e.startswith(".") for e in lang.EXTENSIONS)
    assert isinstance(lang.SERVICE_MARKERS, tuple)
    for fn in ("routes", "imports", "externals"):
        assert callable(getattr(lang, fn))


def test_extensions_are_claimed_once() -> None:
    seen: list[str] = [e for lang in langs.LANGUAGES for e in lang.EXTENSIONS]
    assert len(seen) == len(set(seen))


def test_glob_markers_find_a_service(tmp_path: Path) -> None:
    svc = tmp_path / "Loyalty.Api"
    svc.mkdir()
    (svc / "Loyalty.Api.csproj").write_text("<Project/>")
    assert code_scan.has_marker(svc, ("*.csproj",))
    assert not code_scan.has_marker(tmp_path, ("*.csproj",))
