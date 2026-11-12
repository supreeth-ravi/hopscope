"""hopscope.py all/collect with several regions or accounts: the pipeline reads them through aws_multi.py, and one region
with one profile keeps the original single-scope steps."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import hopscope


def ns(**kw) -> argparse.Namespace:
    base = dict(region="eu-west-1", all_regions=False, profile=None, org_role=None, accounts=None, staging_pattern=None, kube_context=[], kube_contexts=None,
                overrides=None, repo=["."], no_floorplan=False, keep=30)
    return argparse.Namespace(**{**base, **kw})


def steps(monkeypatch, tmp_path: Path, a: argparse.Namespace) -> list[list[str]]:
    seen: list[list[str]] = []
    monkeypatch.setattr(hopscope, "run", lambda script, *args: seen.append([script, *args]))
    monkeypatch.setattr(hopscope, "try_run", lambda script, out, *args: seen.append([script, *args]))
    hopscope.collect(a, tmp_path)
    return seen


def test_single_region_and_profile_keep_the_original_steps(monkeypatch, tmp_path: Path) -> None:
    got = steps(monkeypatch, tmp_path, ns(profile="prod"))
    assert [s[0] for s in got][:5] == ["aws_inventory.py", "code_scan.py", "call_flow.py", "linker.py", "aws_timeseries.py"]
    assert got[0][1:5] == ["--region", "eu-west-1", "--profile", "prod"]


def test_several_regions_go_through_aws_multi(monkeypatch, tmp_path: Path) -> None:
    got = steps(monkeypatch, tmp_path, ns(region="eu-west-1,us-east-1", profile="prod,stage"))
    names = [s[0] for s in got]
    assert names[0] == "aws_multi.py" and "aws_inventory.py" not in names and "aws_timeseries.py" not in names
    first = got[0]
    assert first[first.index("--regions") + 1] == "eu-west-1,us-east-1" and first[first.index("--profiles") + 1] == "prod,stage"
    assert first[first.index("--ts-out") + 1] == str(tmp_path / "ts.json")
    iac = next(s for s in got if s[0] == "iac_scan.py")
    assert iac[iac.index("--region") + 1] == "eu-west-1"


def test_org_role_and_all_regions_are_passed_on(monkeypatch, tmp_path: Path) -> None:
    got = steps(monkeypatch, tmp_path, ns(region=None, all_regions=True, org_role="HopscopeReadOnly", accounts="1,2"))[0]
    assert got[0] == "aws_multi.py" and "--all-regions" in got and got[got.index("--org-role") + 1] == "HopscopeReadOnly"
    assert got[got.index("--accounts") + 1] == "1,2" and "--regions" not in got


def test_multi_scope_needs_a_region_or_all_regions() -> None:
    with pytest.raises(SystemExit):
        hopscope.check_collect(ns(region=None))
    hopscope.check_collect(ns(region=None, all_regions=True))
    with pytest.raises(SystemExit):
        hopscope.check_collect(ns(accounts="1"))  # --accounts only means something with --org-role
