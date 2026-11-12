"""Merging several regions and accounts into one aws.json and ts.json (aws_merge.py): every row tagged with its region and
account, names kept unique by a prefix only where they collide, references renamed with them, global services once."""
from __future__ import annotations

import json

import aws_merge
import scopes


def inv(region: str, *, cluster: str = "kestrel", queue: str = "orders", extra_ecs: bool = False) -> dict:
    tg = "shop-api"
    ecs = [{"cluster": cluster, "n": "shop-api", "running": 2, "desired": 2, "env": "prod", "tgs": [tg], "refs": [], "images": ["shop-api"], "containers": ["api"],
            "cpu": 10, "mem": 20, "memMax": 40, "cpuMax": 30, "size": "", "deploys": []}]
    if extra_ecs:
        ecs.append({**ecs[0], "n": "roaster", "tgs": []})
    return {"ecs": ecs, "ec2": [], "rds": [{"n": "kestrel-db", "instance": "kestrel-db-1", "env": "prod", "serverless": True, "acu": 1, "maxAcu": 4, "conns": 5, "cpu": 1}],
            "lb": [{"n": "kestrel-public", "type": "application", "scheme": "internet-facing", "req": 100, "err": 1, "e503": 0, "t5xx": 0,
                    "routes": [{"port": 443, "when": "default", "tg": tg, "services": [f"{cluster}/shop-api"], "targets": ["healthy"], "instances": []}]}],
            "sqs": [{"n": queue, "sent": 5, "backlog": 0, "age": 0, "dlq": False, "fifo": False, "fam": queue}],
            "lambda": [{"n": "kestrel-grind", "mem": 512, "inv": 3, "err": 0, "dur": 9, "fam": "kestrel-grind"}],
            "sns": [], "cache": [], "search": [], "apigw": [], "cognito": [], "nat": 1, "eips": {"total": 2, "unattached": 1}, "ecr": 3, "secrets": 2,
            "dynamodb": ["kestrel-orders"], "ses": [], "eventbridge": ["nightly"],
            "sfn": [{"n": "refund", "targets": [{"kind": "lambda", "n": "kestrel-grind"}, {"kind": "sqs", "n": queue}], "failed": 0, "started": 1}],
            "kinesis": [], "msk": [], "eks": [],
            "ebTargets": [{"rule": "nightly", "kind": "lambda", "n": "kestrel-grind"}],
            "esm": [{"fn": "kestrel-grind", "kind": "sqs", "source": queue, "state": "Enabled", "last": "OK", "topics": []}]}


def ts(region: str, req: list[float]) -> dict:
    return {"start": "2026-11-01T00:00:00+00:00", "hours": 72,
            "labels": {"lb": "kestrel-public", "db": "kestrel-db", "dbKind": "acu", "dbMax": 4, "mem": "shop-api", "queues": ["orders"]},
            "series": {"req": req, "elb5xx": [1.0] * len(req), "p95": [0.2 if region == "eu-west-1" else 0.5] * len(req), "dbcap": [1.0] * len(req), "mem": [40.0] * len(req),
                       "q0": [2.0] * len(req)},
            "byService": [{"tg": "shop-api", "req": int(sum(req)), "targets": ["healthy"]}]}


GLOBALS = {"kestrel-prod": {"cdn": [{"n": "kestrel-web", "origin": "web", "aliases": []}], "s3": {"count": 4}, "cost": [["Amazon ECS", 100.0], ["Amazon RDS", 50.0]],
                            "dailyCost": [{"d": "2026-11-01", "usd": 10.0}, {"d": "2026-11-02", "usd": 12.0}]}}


def parts_two_regions() -> list[dict]:
    return [{"account": "kestrel-prod", "region": "eu-west-1", "inv": inv("eu-west-1"), "ts": ts("eu-west-1", [10.0, 20.0])},
            {"account": "kestrel-prod", "region": "us-east-1", "inv": inv("us-east-1", queue="orders-us"), "ts": ts("us-east-1", [1.0, 2.0])}]


def test_rows_carry_region_and_account_and_collisions_get_a_region_prefix() -> None:
    aws, _ = aws_merge.merge(parts_two_regions(), GLOBALS, [])
    assert aws["regions"] == ["eu-west-1", "us-east-1"] and aws["accounts"] == ["kestrel-prod"]
    assert all(x["region"] in aws["regions"] and x["account"] == "kestrel-prod" for k in ("ecs", "lb", "sqs", "lambda", "rds", "sfn", "esm") for x in aws[k])
    assert sorted(x["cluster"] for x in aws["ecs"]) == ["eu-west-1:kestrel", "us-east-1:kestrel"]
    keys = {f'{x["cluster"]}/{x["n"]}' for x in aws["ecs"]}
    assert len(keys) == 2
    for lb in aws["lb"]:  # references follow the rename, inside the same region
        assert lb["n"].endswith(":kestrel-public")
        assert lb["routes"][0]["services"] == [f'{lb["region"]}:kestrel/shop-api'] and lb["routes"][0]["tg"] == f'{lb["region"]}:shop-api'
        ecs = next(x for x in aws["ecs"] if x["region"] == lb["region"])
        assert ecs["tgs"] == [lb["routes"][0]["tg"]]
    assert sorted(q["n"] for q in aws["sqs"]) == ["orders", "orders-us"]  # no collision: names untouched
    assert sorted(f["n"] for f in aws["lambda"]) == ["eu-west-1:kestrel-grind", "us-east-1:kestrel-grind"]
    for m in aws["esm"]:
        assert m["fn"] == f'{m["region"]}:kestrel-grind'
    eu_sfn = next(m for m in aws["sfn"] if m["region"] == "eu-west-1")
    assert eu_sfn["n"] == "eu-west-1:refund" and eu_sfn["targets"] == [{"kind": "lambda", "n": "eu-west-1:kestrel-grind"}, {"kind": "sqs", "n": "orders"}]
    assert sorted(aws["dynamodb"]) == ["eu-west-1:kestrel-orders", "us-east-1:kestrel-orders"]
    assert all(t["rule"].endswith(":nightly") and t["n"].endswith(":kestrel-grind") for t in aws["ebTargets"])
    assert scopes.plain("eu-west-1:kestrel-grind") == "kestrel-grind" and scopes.plain("orders") == "orders"


def test_counts_summed_and_global_services_once() -> None:
    aws, _ = aws_merge.merge(parts_two_regions(), GLOBALS, [])
    assert aws["nat"] == 2 and aws["ecr"] == 6 and aws["secrets"] == 4 and aws["eips"] == {"total": 4, "unattached": 2}
    assert aws["s3"] == {"count": 4} and len(aws["cdn"]) == 1 and aws["cost"] == [["Amazon ECS", 100.0], ["Amazon RDS", 50.0]]
    assert aws["region"] == "eu-west-1, us-east-1" and aws["account"] == "kestrel-prod"
    assert aws["where"]["dynamodb"]["us-east-1:kestrel-orders"] == {"region": "us-east-1", "account": "kestrel-prod"}
    assert aws["counts"][0] == {"region": "eu-west-1", "account": "kestrel-prod", "nat": 1, "ecr": 3, "secrets": 2}
    assert aws["scopes"] == [{"account": "kestrel-prod", "region": "eu-west-1"}, {"account": "kestrel-prod", "region": "us-east-1"}]


def test_two_accounts_same_region_prefix_with_the_account_label_and_cost_adds_up() -> None:
    parts = [{"account": "kestrel-prod", "region": "eu-west-1", "inv": inv("eu-west-1"), "ts": ts("eu-west-1", [1.0])},
             {"account": "acct-••••8765", "region": "eu-west-1", "inv": inv("eu-west-1"), "ts": ts("eu-west-1", [2.0])}]
    glob = {**GLOBALS, "acct-••••8765": {"cdn": [], "s3": {"count": 1}, "cost": [["Amazon ECS", 1.0]], "dailyCost": [{"d": "2026-11-02", "usd": 3.0}]}}
    aws, t = aws_merge.merge(parts, glob, [{"account": "acct-••••1111", "region": "eu-west-1", "why": "could not assume the role"}])
    assert sorted(q["n"] for q in aws["sqs"]) == ["acct-••••8765:orders", "kestrel-prod:orders"]
    assert aws["s3"] == {"count": 5} and aws["cost"][0] == ["Amazon ECS", 101.0]
    assert t["dailyCost"] == [{"d": "2026-11-01", "usd": 10.0}, {"d": "2026-11-02", "usd": 15.0}]
    assert aws["account"] == "2 accounts" and aws["skippedScopes"][0]["account"] == "acct-••••1111"


def test_time_series_add_traffic_and_keep_the_worst_latency_with_unique_labels() -> None:
    _, t = aws_merge.merge(parts_two_regions(), GLOBALS, [])
    s, lab = t["series"], t["labels"]
    assert s["req"] == [11.0, 22.0] and s["elb5xx"] == [2.0, 2.0] and s["p95"] == [0.5, 0.5]
    assert lab["lb"] == "2 load balancers in 2 regions"
    assert lab["db"] in ("eu-west-1:kestrel-db", "us-east-1:kestrel-db") and lab["mem"] == "shop-api"
    assert lab["queues"][0] in ("orders", "orders-us") and len(lab["queues"]) == 2 and "q1" in s
    assert sorted(b["tg"] for b in t["byService"]) == ["eu-west-1:shop-api", "us-east-1:shop-api"] and t["byService"][0]["req"] == 30
    assert t["dailyCost"] == GLOBALS["kestrel-prod"]["dailyCost"]


def test_single_scope_keeps_its_own_labels() -> None:
    p = parts_two_regions()[:1]
    aws, t = aws_merge.merge(p, GLOBALS, [])
    assert aws["ecs"][0]["cluster"] == "kestrel" and t["labels"]["lb"] == "kestrel-public" and t["series"]["req"] == [10.0, 20.0]


def test_no_account_number_in_merged_output() -> None:
    acct = "5" * 12
    p = parts_two_regions()
    p[0]["inv"]["ecs"][0]["deploys"] = [{"kind": "event", "at": "x", "msg": f"arn:aws:iam::{acct}:role/x"}]
    aws, t = aws_merge.merge(p, GLOBALS, [])
    assert acct not in json.dumps(aws) + json.dumps(t)
