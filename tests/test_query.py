"""hopscope_query: answers about routes, files and resources from the demo data (Kestrel Coffee, invented)."""
import ast
import json
import shutil
import sys
from pathlib import Path

import pytest

import hopscope_query as q

DEMO = Path(__file__).resolve().parents[1] / "docs" / "demo"


@pytest.fixture(scope="module")
def data() -> dict:
    return q.load(DEMO)


def test_load_needs_code_and_model(tmp_path: Path) -> None:
    with pytest.raises(q.NoData) as e:
        q.load(tmp_path)
    assert "hopscope.py design" in str(e.value)


@pytest.mark.parametrize("name", ["hopscope_query.py", "hopscope_callers.py"])
def test_stdlib_only_and_no_side_effects(name: str) -> None:
    tree = ast.parse((Path(q.__file__).parent / name).read_text())
    mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and n.level == 0}
    assert mods <= set(sys.stdlib_module_names) | {"__future__", "hopscope_callers"}
    assert not mods & {"subprocess", "socket", "urllib", "http", "os", "shutil"}


def test_find_routes_by_method_and_path(data: dict) -> None:
    hits = q.find_routes(data, "POST /account/sign-in")
    assert hits["routes"][0]["route"] == "POST /account/sign-in"
    words = q.find_routes(data, "sign in")
    assert words["routes"][0]["route"] == "POST /account/sign-in"
    assert len(q.find_routes(data, "account", limit=1)["routes"]) == 1


def test_find_routes_ranks_whole_path_segments_first(data: dict) -> None:
    assert q.find_routes(data, "checkout")["routes"][0]["route"] == "POST /cart/checkout"
    assert q.find_routes(data, "skip")["routes"][0]["route"] == "POST /subscriptions/{sub_id}/skip"
    assert q.find_routes(data, "post stock")["routes"][0]["route"] == "POST /beans/:id/stock"
    top = q.find_routes(data, "orders")["routes"]
    assert top[0]["route"] == "GET /orders" and top[0]["score"] > top[1]["score"]


def test_trace_free_text_follows_a_clear_best_match(data: dict) -> None:
    t = q.trace(data, "checkout")
    assert t["route"] == "POST /cart/checkout" and "checkout" in t["matched"]
    assert "matched" not in q.trace(data, "POST /cart/checkout")


def test_trace_free_text_lists_close_candidates(data: dict) -> None:
    t = q.trace(data, "account")
    assert "2 routes match" in t["error"]
    assert {s["route"] for s in t["suggestions"]} == {"POST /account/sign-in", "POST /account/sign-up"}
    assert "error" in q.trace(data, "POST /orders")  # an explicit method and path is never swapped for another route


def test_trace_sign_in(data: dict) -> None:
    t = q.trace(data, "POST /account/sign-in")
    assert t["route"] == "POST /account/sign-in"
    assert t["handler"] == "shop-api/app/routes/account.py:18"
    assert t["service"] == "shop-api"
    assert "shop-api" in t["compute"]
    assert t["entry"] and t["entry"][0]["lb"] == "kestrel-public"
    labels = [s["label"] for s in t["steps"]]
    assert "Cognito: InitiateAuth" in labels
    cognito = next(s for s in t["steps"] if s["label"] == "Cognito: InitiateAuth")
    assert cognito["at"] == "shop-api/app/identity.py:8" and cognito["kind"] == "aws"
    assert t["database"] == "kestrel-db"  # an SQL step reaches the primary database


def test_trace_conditional_queue_and_worker(data: dict) -> None:
    t = q.trace(data, "POST /webhooks/payments")
    pub = [s for s in t["steps"] if s["label"].startswith("SQS")]
    assert pub and pub[0]["runs"] == "conditional" and "charge.refunded" in pub[0]["when"]
    assert t["queues"] == [{"queue": "kestrel-orders.fifo", "worker": "order-worker", "job": "Postgres: write result", "basis": "service publishes to"}]


def test_trace_accepts_index_and_reports_unknown(data: dict) -> None:
    assert q.trace(data, "10")["route"] == "POST /account/sign-in"
    miss = q.trace(data, "DELETE /nothing")
    assert "error" in miss and isinstance(miss["suggestions"], list)


def test_who_calls_file_is_transitive(data: dict) -> None:
    w = q.who_calls(data, "app/db.py")
    callers = {c["file"]: c["depth"] for c in w["callers"]}
    assert callers["shop-api/app/routes/cart.py"] == 1
    assert callers["shop-api/app/main.py"] == 2  # main imports the routes that import db
    assert any(r["route"] == "POST /cart/checkout" for r in w["routes"])


def test_who_calls_symbol(data: dict) -> None:
    w = q.who_calls(data, "check_password")
    assert w["files"] == ["shop-api/app/identity.py"]
    assert [r["route"] for r in w["routes"]] == ["POST /account/sign-in"]


def test_what_runs(data: dict) -> None:
    w = q.what_runs(data, "shop-api")
    assert [c["name"] for c in w["compute"]][0] == "shop-api"
    assert w["queues"][0]["queue"] == "kestrel-orders.fifo"
    assert w["routes"] == 9
    assert q.what_runs(data, "shop-api/app/routes/cart.py")["service"] == "shop-api"
    assert "error" in q.what_runs(data, "nope")


def test_impact_identity(data: dict) -> None:
    i = q.impact(data, "shop-api/app/identity.py")
    assert i["matched"] == ["shop-api/app/identity.py"]
    assert {x["file"] for x in i["importers"]} >= {"shop-api/app/routes/account.py", "shop-api/app/main.py"}
    routes = [r["route"] for r in i["routes"]]
    assert routes[:2] == ["POST /account/sign-in", "POST /account/sign-up"]  # flow steps into the file rank first
    assert i["routes"][0]["distance"] == 0
    assert any(s["service"] == "shop-api" and "shop-api" in s["compute"] for s in i["services"])
    again = q.impact(data, "app/identity.py")  # suffix match, same answer every time
    assert {**again, "path": i["path"]} == i


def test_impact_reaches_queue_workers(data: dict) -> None:
    i = q.impact(data, "shop-api/app/queue.py")
    assert "POST /cart/checkout" in [r["route"] for r in i["routes"]]
    assert {"queue": "kestrel-orders.fifo", "worker": "order-worker", "job": "Postgres: write result", "basis": "service publishes to"} in i["queues"]
    assert "order-worker" in i["workers"]


def test_impact_unknown_file(data: dict) -> None:
    assert "error" in q.impact(data, "no/such.py")


def test_resources_and_health(data: dict) -> None:
    r = q.resources(data, "sqs")
    assert all(x["kind"] == "sqs" for x in r["rows"])
    assert q.resources(data)["aws"]["lambda"] > 0
    h = q.health(data, "kestrel-orders-dlq.fifo")
    assert h["rows"][0]["status"] == "bad" and h["actions"]
    assert h["mode"] == "live"


def test_health_design_mode_says_no_numbers(tmp_path: Path) -> None:
    for f in DEMO.glob("*.json"):
        shutil.copy(f, tmp_path / f.name)
    m = json.loads((tmp_path / "model.json").read_text())
    m["meta"]["mode"] = "design"
    (tmp_path / "model.json").write_text(json.dumps(m))
    h = q.health(q.load(tmp_path), "shop-api")
    assert h["mode"] == "design" and "no live numbers" in h["note"]
    assert "cpu" not in h["rows"][0]


def test_results_are_capped_and_serialisable(data: dict) -> None:
    long = "x" * 5000
    out = q.find_routes(data, long, limit=500)
    assert len(out["query"]) <= 300 and len(out["routes"]) <= 50
    json.dumps(q.impact(data, "shop-api/app/db.py"))


@pytest.mark.parametrize("target", ["/Users/someone/proj/shop-api/app/identity.py", "app\\identity.py", "shop-api\\app\\identity.py",
                                    "./shop-api/app/identity.py", "C:\\work\\proj\\shop-api\\app\\identity.py"])
def test_match_files_absolute_and_windows_paths(data: dict, target: str) -> None:
    i = q.impact(data, target)
    assert i["matched"] == ["shop-api/app/identity.py"], i
    assert "/Users/someone" not in json.dumps(i["matched"]) and data["code"]["base"] not in json.dumps(i)


def test_match_folder_targets(data: dict) -> None:
    for target in ("shop-api/app/routes/", "app/routes", "/home/me/proj/shop-api/app/routes"):
        i = q.impact(data, target)
        assert set(i["matched"]) == {"shop-api/app/routes/" + f for f in ("cart.py", "subscriptions.py", "__init__.py", "orders.py", "webhooks.py", "account.py", "catalog.py")}
    assert "error" in q.impact(data, "app/rout")  # folders match on whole segments only
    assert q.who_calls(data, "app\\db.py")["files"] == ["shop-api/app/db.py"]


def test_queues_are_per_route_with_their_basis(data: dict) -> None:
    t = q.trace(data, "POST /cart/checkout")
    assert t["queues"] == [{"queue": "kestrel-orders.fifo", "worker": "order-worker", "job": "Postgres: write result", "basis": "service publishes to"}]
    assert q.trace(data, "POST /account/sign-in")["queues"] == []


def test_queue_named_in_the_step_is_used(tmp_path: Path) -> None:
    for f in DEMO.glob("*.json"):
        shutil.copy(f, tmp_path / f.name)
    m = json.loads((tmp_path / "model.json").read_text())
    m["queues"]["shop-api"] = ["kestrel-orders.fifo", "kestrel-audit.fifo"]
    (tmp_path / "model.json").write_text(json.dumps(m))
    fl = json.loads((tmp_path / "flows.json").read_text())
    fl["5"] = [s if not str(s[2]).startswith("SQS") else [*s[:2], "SQS: SendMessage kestrel-audit", *s[3:]] for s in fl["5"]]
    (tmp_path / "flows.json").write_text(json.dumps(fl))
    t = q.trace(q.load(tmp_path), "POST /cart/checkout")
    assert t["queues"] == [{"queue": "kestrel-audit.fifo", "worker": None, "job": None, "basis": "step"}]
    both = q.trace(q.load(DEMO), "POST /cart/checkout")
    assert both["queues"][0]["basis"] == "service publishes to"


def test_lists_are_capped(tmp_path: Path) -> None:
    for f in DEMO.glob("*.json"):
        shutil.copy(f, tmp_path / f.name)
    m = json.loads((tmp_path / "model.json").read_text())
    many = [f"svc-{i}" for i in range(200)]
    m["placement"]["shop-api"]["compute"] = many
    m["infra"] += [{"n": f"x{i}", "kind": f"kind{i}"} for i in range(200)]
    (tmp_path / "model.json").write_text(json.dumps(m))
    a = json.loads((tmp_path / "aws.json").read_text())
    a.update({f"svc{i}": [] for i in range(200)})
    (tmp_path / "aws.json").write_text(json.dumps(a))
    d = q.load(tmp_path)
    assert len(q.trace(d, "POST /account/sign-in")["compute"]) == q.MAX_ITEMS
    r = q.resources(d)
    assert len(r["kinds"]) <= q.MAX_ITEMS and len(r["aws"]) <= q.MAX_ITEMS
    assert all(len(s["compute"]) <= q.MAX_ITEMS for s in q.impact(d, "app/identity.py")["services"])
