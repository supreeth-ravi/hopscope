"""Call flows for Java, Kotlin and C# route handlers (langs/braceflow.py) and their wiring into call_flow.py."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import code_scan
from kestrel_fixtures import LOYALTY, ROASTER, write
from langs import braceflow, flowkinds

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "hopscope" / "scripts"


def _data(base: Path) -> dict:
    cm = code_scan.scan([base])
    return {"base": str(base), "files": cm.files, "routes": cm.routes, "services": cm.services}


def _flow(data: dict, method: str, path: str) -> list[list]:
    i = next(i for i, r in enumerate(data["routes"]) if r["m"] == method and r["p"] == path)
    steps = braceflow.flows(data, Path(data["base"]), i)
    assert steps is not None
    return steps


def _short(steps: list[list]) -> list[tuple]:
    return [(s[0], s[1], s[2], s[5], s[6]) for s in steps]


@pytest.fixture()
def roaster(tmp_path: Path) -> dict:
    return _data(write(tmp_path, ROASTER))


@pytest.fixture()
def loyalty(tmp_path: Path) -> dict:
    return _data(write(tmp_path, LOYALTY))


def test_spring_flow_follows_injected_service_with_conditions(roaster: dict) -> None:
    steps = _short(_flow(roaster, "POST", "/api/roasts/{id}/start"))
    assert steps == [
        (0, "call", "start()", 0, ""),
        (1, "call", "find()", 0, ""),
        (2, "sql", "Postgres: SELECT by id Roast", 0, ""),
        (1, "kafka", "Kafka: send to roast-events", 1, "roast is ready"),
        (1, "http", "HTTP POST http://loyalty-api/api/points", 1, "otherwise"),
        (1, "aws", "SQS: SendMessage", 0, ""),
        (1, "cache", "Cache: SET", 2, "a sdk error happens"),
        (1, "sql", "Postgres: UPSERT Roast", 0, ""),
    ]


def test_flow_steps_point_at_files_and_lines(roaster: dict) -> None:
    steps = _flow(roaster, "DELETE", "/api/roasts/{id}")
    files = [f["p"] for f in roaster["files"]]
    call, sql = steps
    assert files[call[3]].endswith("service/RoastService.java") and call[4] == 49
    assert sql[1:3] == ["sql", "Postgres: DELETE Roast"] and sql[7] == "cancel"


def test_kotlin_expression_bodies(roaster: dict) -> None:
    assert _short(_flow(roaster, "GET", "/api/beans")) == [(0, "call", "all()", 0, "")]


def test_csharp_controller_flow_resolves_interface_to_class(loyalty: dict) -> None:
    steps = _short(_flow(loyalty, "POST", "/api/Members/{id}/points"))
    assert steps == [
        (0, "call", "AddPointsAsync()", 0, ""),
        (1, "sql", "SQL Server: SELECT by id Members + commit", 0, ""),
        (1, "aws", "SES: SendEmail", 0, ""),
        (1, "http", "HTTP POST http://roaster-service/api/roasts/1/start", 2, "a amazon simple email service error happens"),
    ]
    get = _short(_flow(loyalty, "GET", "/api/Members/{id:int}"))
    assert get == [(0, "call", "FindAsync()", 0, ""), (1, "cache", "Cache: GET", 0, ""), (1, "sql", "SQL Server: SELECT by id Members", 0, "")]


def test_minimal_api_lambdas_and_method_groups(loyalty: dict) -> None:
    assert _short(_flow(loyalty, "GET", "/api/rewards")) == [(0, "sql", "SQL Server: SELECT Members", 0, "")]
    assert _short(_flow(loyalty, "POST", "/api/rewards/{code}/redeem")) == [(0, "kafka", "Kafka: send to rewards-redeemed", 0, "")]


@pytest.mark.parametrize("cond, words", [
    ("member == null", "member is missing"), ("order != null", "order is set"), ("roast.isReady()", "roast is ready"),
    ("!roast.isReady()", "roast is not ready"), ("member.Points > 100", "points is above 100"), ("items.isEmpty()", "items is empty"),
    ("!items.isEmpty()", "items is not empty"), ("a(b) + c", ""), ("string.IsNullOrEmpty(code)", "code is missing"),
])
def test_conditions_in_words(cond: str, words: str) -> None:
    assert flowkinds.phrase(cond) == words


def test_call_flow_script_writes_java_and_csharp_flows(tmp_path: Path) -> None:
    repo = write(tmp_path / "kestrel", ROASTER, LOYALTY)
    code, flows = tmp_path / "code.json", tmp_path / "flows.json"
    subprocess.run([sys.executable, str(SCRIPTS / "code_scan.py"), "--repo", str(repo), "--no-floorplan", "--out", str(code)], check=True, capture_output=True)
    subprocess.run([sys.executable, str(SCRIPTS / "call_flow.py"), str(code), str(flows)], check=True, capture_output=True)
    data, got = json.loads(code.read_text()), json.loads(flows.read_text())
    start = next(i for i, r in enumerate(data["routes"]) if r["p"] == "/api/roasts/{id}/start")
    assert [s[1] for s in got[str(start)]] == ["call", "call", "sql", "kafka", "http", "aws", "cache", "sql"]
    assert all(len(s) == 8 for steps in got.values() for s in steps)


def test_query_trace_marks_java_handlers_as_traced() -> None:
    import hopscope_query as q
    data = {"code": {"files": [{"p": "svc/A.java"}], "routes": [{"m": "GET", "p": "/a", "f": 0, "l": 3}], "services": ["svc"]},
            "flows": {}, "model": {}, "aws": {}, "links": {}}
    assert q.trace(data, "GET /a")["traced"] is True
