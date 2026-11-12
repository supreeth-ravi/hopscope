"""Links below 0.6 confidence are reported as unconfirmed wherever an answer says which compute runs which code."""
import copy
import json
import shutil
from pathlib import Path

import pytest

import drift
import hopscope_changes
import hopscope_export
import hopscope_mcp
import hopscope_query as q
import hopscope_text

DEMO = Path(__file__).resolve().parents[1] / "docs" / "demo"
KEY = "prod-kestrel/analytics-engine"
LOW = {"name": "analytics-engine", "confidence": 0.5, "unconfirmed": True}
SAY = "analytics-engine (unconfirmed link, 0.5 — confirm with hopscope.py review)"


def with_low_link(data: dict) -> dict:
    """The demo plus an invented analytics-engine service the linker placed on shop-api with confidence 0.5."""
    d = copy.deepcopy(data)
    d["model"]["infra"].append({"n": "analytics-engine", "key": KEY, "sys": "shop-api", "kind": "ecs", "env": "prod", "tasks": 1, "size": "0.25 vCPU"})
    d["model"]["placement"]["shop-api"]["compute"].append("analytics-engine")
    d["model"]["review"] = [{"ecs": KEY, "code": "shop-api", "confidence": 0.5, "evidence": ["name only"]}]
    return d


@pytest.fixture(scope="module")
def low() -> dict:
    return with_low_link(q.load(DEMO))


def test_trace_marks_the_low_confidence_compute(low: dict) -> None:
    t = q.trace(low, "POST /account/sign-in")
    assert "shop-api" in t["compute"]  # confirmed links keep their plain shape
    assert LOW in t["compute"]
    assert "unconfirmed" in t["note"] and "hopscope.py review" in t["note"]
    assert "note" not in q.trace(q.load(DEMO), "POST /account/sign-in")


def test_what_runs_impact_and_health_carry_the_confidence(low: dict) -> None:
    w = {c["name"]: c for c in q.what_runs(low, "shop-api")["compute"]}
    assert w["analytics-engine"]["confidence"] == 0.5 and w["analytics-engine"]["unconfirmed"] is True
    assert "unconfirmed" not in w["shop-api"]
    services = {s["service"]: s["compute"] for s in q.impact(low, "app/identity.py")["services"]}
    assert LOW in services["shop-api"] and "shop-api" in services["shop-api"]
    row = q.health(low, "analytics-engine")["rows"][0]
    assert row["sys"] == "shop-api" and row["confidence"] == 0.5 and row["unconfirmed"] is True
    assert "unconfirmed" not in q.health(low, "shop-api")["rows"][0]


def test_links_json_confidence_and_hopscope_json_overrides(low: dict) -> None:
    d = copy.deepcopy(low)
    d["model"]["review"] = []
    d["links"] = {"placement": {KEY: {"code": "shop-api", "confidence": 0.4}, "prod-kestrel/shop-api": {"code": "shop-api", "confidence": 1.0}}}
    t = q.trace(d, "POST /account/sign-in")
    assert {"name": "analytics-engine", "confidence": 0.4, "unconfirmed": True} in t["compute"] and "shop-api" in t["compute"]
    assert {c["name"]: c.get("confidence") for c in q.what_runs(d, "shop-api")["compute"]}["shop-api"] == 1.0
    d["links"]["placement"][KEY] = {"code": "shop-api", "confidence": 1.0, "evidence": ["set in hopscope.json"]}
    assert "analytics-engine" in q.trace(d, "POST /account/sign-in")["compute"]


def test_untrusted_confidence_values_do_not_break_answers(low: dict) -> None:
    d = copy.deepcopy(low)
    d["model"]["review"] = [{"ecs": KEY, "confidence": "high"}, "junk", {"ecs": KEY, "confidence": None}]
    assert "analytics-engine" in q.trace(d, "POST /account/sign-in")["compute"]


def test_cli_text_says_unconfirmed(low: dict) -> None:
    assert f"runs on: shop-api, shop-api (staging-kestrel), {SAY}" in hopscope_text.trace_text(q.trace(low, "POST /account/sign-in"))
    assert SAY in hopscope_text.impact_text(q.impact(low, "app/identity.py"))
    assert "unconfirmed link" not in hopscope_text.trace_text(q.trace(q.load(DEMO), "POST /account/sign-in"))


def test_export_names_an_unconfirmed_compute(low: dict) -> None:
    d = copy.deepcopy(low)
    d["model"]["placement"]["shop-api"]["compute"] = ["analytics-engine"]
    assert "analytics-engine (unconfirmed link)" in hopscope_export.mermaid_trace(d, "POST /account/sign-in")


def test_drift_link_findings_carry_the_confidence(tmp_path: Path) -> None:
    files = {"ecs": [{"cluster": "local", "n": "admin", "build": "admin", "source": "compose", "buildFrom": "docker-compose.yml"}], "sources": ["docker-compose.yml"]}
    code = {"base": str(tmp_path), "services": ["api", "admin"], "files": []}
    rep = drift.report(code, {"placement": {"local/admin": {"code": "api", "confidence": 0.5, "evidence": ["name only"]}}}, None, files)
    f = next(x for x in rep["findings"] if x["kind"] == "link_may_be_wrong")
    assert f["linkConfidence"] == 0.5 and f["unconfirmed"] is True and f["confidence"] == "low"
    sure = drift.report(code, {"placement": {"local/admin": {"code": "api", "confidence": 0.9}}}, None, files)
    g = next(x for x in sure["findings"] if x["kind"] == "link_may_be_wrong")
    assert g["linkConfidence"] == 0.9 and "unconfirmed" not in g
    out = hopscope_changes.drift({"drift": rep})
    mine = next(x for x in out["findings"] if x["kind"] == "link_may_be_wrong")
    assert mine["linkConfidence"] == 0.5 and mine["unconfirmed"] is True
    assert "unconfirmed link, 0.5 — confirm with hopscope.py review" in hopscope_changes.drift_text(out)


def test_mcp_descriptions_tell_agents_to_say_unconfirmed() -> None:
    for name in ("trace", "what_runs", "impact", "health", "drift"):
        assert "unconfirmed" in hopscope_mcp.BY_NAME[name]["description"], name
    assert "unconfirmed" in hopscope_mcp.INSTRUCTIONS


def test_mcp_trace_returns_the_unconfirmed_link(tmp_path: Path) -> None:
    d = tmp_path / ".hopscope" / "data"
    d.mkdir(parents=True)
    for f in DEMO.glob("*.json"):
        shutil.copy(f, d / f.name)
    model = json.loads((d / "model.json").read_text())
    model["infra"].append({"n": "analytics-engine", "key": KEY, "sys": "shop-api", "kind": "ecs", "env": "prod"})
    model["placement"]["shop-api"]["compute"].append("analytics-engine")
    model["review"] = [{"ecs": KEY, "code": "shop-api", "confidence": 0.5, "evidence": []}]
    (d / "model.json").write_text(json.dumps(model))
    res = hopscope_mcp.call_tool(tmp_path / ".hopscope", {"name": "trace", "arguments": {"route": "POST /account/sign-in"}})
    assert res["isError"] is False and LOW in res["structuredContent"]["compute"]


def test_console_marks_unconfirmed_links() -> None:
    html = (Path(__file__).resolve().parents[1] / "skills" / "hopscope" / "assets" / "console.html").read_text()
    assert "unconfirmed link" in html and "M.review" in html
