"""Linking, streams, drift names and the model keep working on a merged inventory whose rows carry a region and an account
and whose shared names carry a scope prefix ("eu-west-1:orders")."""
from __future__ import annotations

import json
from pathlib import Path

import drift_names
import linker
import linker_streams
import rules


def ecs(cluster: str, n: str, region: str, refs: tuple = ()) -> dict:
    return {"cluster": cluster, "n": n, "images": [n], "containers": [n], "refs": list(refs), "env": "prod", "running": 1, "region": region,
            "account": "kestrel-prod", "cpu": 1, "mem": 1, "memMax": 1, "cpuMax": 1, "size": "", "tgs": []}


def url(region: str, q: str) -> str:
    return f"https://sqs.{region}.amazonaws.com/{'*' * 12}/{q}"


def merged() -> dict:
    return {"ecs": [ecs("eu-west-1:kestrel", "orders-worker", "eu-west-1", ({"k": "QUEUE_URL", "v": url("eu-west-1", "order-events")},)),
                    ecs("us-east-1:kestrel", "orders-worker", "us-east-1", ({"k": "QUEUE_URL", "v": url("us-east-1", "order-events")},)),
                    ecs("eu-west-1:kestrel", "shop-api", "eu-west-1")],
            "sqs": [{"n": f"{r}:order-events", "sent": 3, "backlog": 0, "region": r, "account": "kestrel-prod"} for r in ("eu-west-1", "us-east-1")],
            "kinesis": [{"n": f"{r}:clickstream", "region": r} for r in ("eu-west-1", "us-east-1")], "sfn": [], "esm": [], "ec2": [], "lb": [], "rds": []}


def code(tmp: Path) -> dict:
    files = []
    for svc, text in (("orders", "import boto3\nQ = 'order-events'\nsqs.receive_message(QueueUrl=Q)\n"), ("shop", "kinesis.put_record(StreamName='clickstream')\n")):
        (tmp / svc).mkdir()
        (tmp / svc / "main.py").write_text(text)
        files.append({"p": f"{svc}/main.py", "x": ["kinesis"] if svc == "shop" else ["sqs"]})
    return {"base": str(tmp), "services": ["orders", "shop"], "files": files}


def test_placement_ignores_region_prefixes_in_cluster_names(tmp_path: Path) -> None:
    c = code(tmp_path)
    placement = linker.score_placement(c, merged(), [], c["services"])
    assert placement["eu-west-1:kestrel/orders-worker"]["code"] == "orders" and placement["us-east-1:kestrel/orders-worker"]["code"] == "orders"
    assert not any("east" in e or "west" in e for p in placement.values() for e in p["evidence"])


def test_queues_match_by_plain_name_and_url_in_the_same_region(tmp_path: Path) -> None:
    c = code(tmp_path)
    inv = merged()
    placement = {"eu-west-1:kestrel/orders-worker": {"code": "orders", "confidence": 1.0}, "us-east-1:kestrel/orders-worker": {"code": "orders", "confidence": 1.0},
                 "eu-west-1:kestrel/shop-api": {"code": "shop", "confidence": 1.0}}
    queues, workers = linker.queue_links(c, inv, c["services"], placement, tmp_path)
    assert queues["orders"] == ["eu-west-1:order-events", "us-east-1:order-events"]
    assert workers["eu-west-1:order-events"][0] == "orders-worker" and workers["us-east-1:order-events"][0] == "orders-worker"


def test_streams_match_by_plain_name(tmp_path: Path) -> None:
    c = code(tmp_path)
    streams, _ = linker_streams.stream_links(c, merged(), c["services"], {}, tmp_path)
    assert streams["shop"] == ["kinesis:eu-west-1:clickstream", "kinesis:us-east-1:clickstream"]


def test_drift_names_compare_without_the_scope_prefix() -> None:
    assert drift_names.level("eu-west-1:order-events", "order-events") == "exact"
    assert drift_names.canon("kestrel-prod:eu-west-1:staging-orders") == ["orders"]


def test_model_rows_carry_region_and_account() -> None:
    inv = {**merged(), "rds": [{"n": "kestrel-db", "serverless": False, "class": "db.t4g.small", "cpu": 3, "conns": 2, "acu": 0, "env": "prod", "region": "eu-west-1",
                                "account": "kestrel-prod"}], "ec2": []}
    rows, _ = rules.infra_entries(inv, {"placement": {}, "labels": {}})
    assert {(r["n"], r.get("region")) for r in rows} >= {("orders-worker (eu-west-1:kestrel)", "eu-west-1"), ("kestrel-db", "eu-west-1")}
    assert all(r.get("account") == "kestrel-prod" for r in rows)
    assert json.dumps(rows)


def test_resources_answer_says_where_each_row_runs() -> None:
    import hopscope_query

    data = {"model": {"meta": {"mode": "live"}, "infra": [{"n": "kestrel-db", "kind": "rds", "env": "prod", "sys": "data", "region": "eu-west-1", "account": "kestrel-prod"}]},
            "aws": {"regions": ["eu-west-1", "us-east-1"], "accounts": ["kestrel-prod"], "scopes": [], "skippedScopes": [], "sqs": [{"n": "a"}]}}
    got = hopscope_query.resources(data)
    assert got["rows"][0]["region"] == "eu-west-1" and got["rows"][0]["account"] == "kestrel-prod"
    assert "regions" not in got["aws"] and "scopes" not in got["aws"]


def test_console_filters_by_region_and_account_and_escapes_them() -> None:
    html = (Path(__file__).resolve().parents[1] / "skills" / "hopscope" / "assets" / "console.html").read_text()
    assert 'data-scope="${key}" data-val="${esc(v)}"' in html and "${esc(v)}</button>" in html
    assert "const scopePill = (r) => (whereText(r) ? ` <span class=\"pill mute\">${esc(whereText(r))}</span>` : '');" in html
    assert "esc([r.account, r.region].filter(Boolean).join(' · '))" in html  # the drawer says where a resource runs
    assert "inScopeView(renderAwsArch)" in html and "inScopeView(renderComponents)" in html
    assert "{ rlabel: true, title: `Region ${lastRegion || 'unknown'}` }" in html and "${esc(n.title)}</text>" in html
