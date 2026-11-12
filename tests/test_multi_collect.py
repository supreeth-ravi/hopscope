"""One run over several regions and accounts (aws_multi.py): bounded parallel collection, regions with nothing in them
skipped, a failing region or account never fails the run, global services read once per account. Stubbed only."""
from __future__ import annotations

import json
import threading

import aws_accounts
import aws_inventory
import aws_multi
import aws_timeseries


def empty_inv() -> dict:
    return {"ecs": [], "ec2": [], "rds": [], "lb": [], "sqs": [], "lambda": [], "sns": [], "cache": [], "search": [], "apigw": [], "cognito": [],
            "nat": 0, "eips": {"total": 0, "unattached": 0}, "ecr": 0, "secrets": 0, "dynamodb": [], "ses": [], "eventbridge": [],
            "sfn": [], "kinesis": [], "msk": [], "ebTargets": [], "esm": [], "eks": []}


class Sess:
    def __init__(self, label: str, region: str | None):
        self.label, self.region = label, region


def fake_regional(s, staging, kube=None, include_global=True, failures=None):
    assert include_global is False and not kube
    if s.region == "ap-south-1":
        raise RuntimeError("endpoint unreachable")
    inv = empty_inv()
    if s.region == "eu-north-1":
        return inv  # nothing deployed there
    inv["sqs"] = [{"n": "orders", "sent": 1, "backlog": 0, "age": 0, "dlq": False, "fifo": False, "fam": "orders"}]
    inv["ecr"] = 1
    return inv


def test_collect_all_skips_empty_and_failing_regions(monkeypatch, capsys) -> None:
    calls: list[tuple] = []
    active, peak = [0], [0]
    lock = threading.Lock()

    def regional(s, staging, kube=None, include_global=True, failures=None):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            calls.append((s.label, s.region))
            return fake_regional(s, staging, kube, include_global)
        finally:
            with lock:
                active[0] -= 1

    monkeypatch.setattr(aws_inventory, "regional", regional)
    monkeypatch.setattr(aws_inventory, "collect_global", lambda s: {"cdn": [], "s3": {"count": 2}})
    monkeypatch.setattr(aws_inventory, "collect_cost", lambda s: [["Amazon SQS", 3.0]])
    monkeypatch.setattr(aws_timeseries, "hourly", lambda s, inv: {"start": "t", "hours": 72, "labels": {"queues": ["orders"]}, "series": {"q0": [1.0]}, "byService": []})
    monkeypatch.setattr(aws_timeseries, "daily_cost", lambda s: [{"d": "2026-11-01", "usd": 1.0}])
    accts = [aws_accounts.Account("kestrel-prod", lambda r: Sess("kestrel-prod", r)), aws_accounts.Account("acct-••••8765", lambda r: Sess("acct-••••8765", r))]
    regions = ["eu-west-1", "us-east-1", "eu-north-1", "ap-south-1"]
    parts, glob, skipped = aws_multi.collect_all(accts, lambda a: regions, None, workers=3)
    assert [(p["account"], p["region"]) for p in parts] == [("kestrel-prod", "eu-west-1"), ("kestrel-prod", "us-east-1"),
                                                           ("acct-••••8765", "eu-west-1"), ("acct-••••8765", "us-east-1")]
    assert {(s["account"], s["region"]) for s in skipped} == {(a, r) for a in ("kestrel-prod", "acct-••••8765") for r in ("eu-north-1", "ap-south-1")}
    why = {s["region"]: s["why"] for s in skipped}
    assert why["eu-north-1"] == "nothing found" and "RuntimeError" in why["ap-south-1"]
    assert set(glob) == {"kestrel-prod", "acct-••••8765"} and glob["kestrel-prod"]["s3"] == {"count": 2}
    assert len(calls) == 8 and 1 <= peak[0] <= 3
    assert "ap-south-1 skipped" in capsys.readouterr().out


def test_regions_listed_per_account_when_all_regions(monkeypatch) -> None:
    monkeypatch.setattr(aws_inventory, "regional", fake_regional)
    monkeypatch.setattr(aws_inventory, "collect_global", lambda s: {"cdn": [], "s3": {"count": 0}})
    monkeypatch.setattr(aws_inventory, "collect_cost", lambda s: [])
    monkeypatch.setattr(aws_timeseries, "hourly", lambda s, inv: {"labels": {}, "series": {}, "byService": []})
    monkeypatch.setattr(aws_timeseries, "daily_cost", lambda s: [])
    acct = aws_accounts.Account("kestrel-prod", lambda r: Sess("kestrel-prod", r))
    parts, _, _ = aws_multi.collect_all([acct], lambda a: ["us-east-1"], None)
    assert [p["region"] for p in parts] == ["us-east-1"]


def test_time_series_failure_keeps_the_inventory(monkeypatch, capsys) -> None:
    monkeypatch.setattr(aws_inventory, "regional", fake_regional)
    monkeypatch.setattr(aws_inventory, "collect_global", lambda s: {"cdn": [], "s3": {"count": 0}})
    monkeypatch.setattr(aws_inventory, "collect_cost", lambda s: [])
    monkeypatch.setattr(aws_timeseries, "daily_cost", lambda s: [])

    def broken(s, inv):
        raise KeyError("LoadBalancers")

    monkeypatch.setattr(aws_timeseries, "hourly", broken)
    acct = aws_accounts.Account("kestrel-prod", lambda r: Sess("kestrel-prod", r))
    parts, _, skipped = aws_multi.collect_all([acct], lambda a: ["eu-west-1"], None)
    assert parts[0]["inv"]["sqs"] and parts[0]["ts"]["series"] == {} and not skipped
    assert "time series" in capsys.readouterr().out


def test_cli_writes_merged_files(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(aws_inventory, "regional", fake_regional)
    monkeypatch.setattr(aws_inventory, "collect_global", lambda s: {"cdn": [], "s3": {"count": 1}})
    monkeypatch.setattr(aws_inventory, "collect_cost", lambda s: [])
    monkeypatch.setattr(aws_timeseries, "hourly", lambda s, inv: {"labels": {"queues": ["orders"]}, "series": {"q0": [2.0]}, "byService": []})
    monkeypatch.setattr(aws_timeseries, "daily_cost", lambda s: [])
    monkeypatch.setattr(aws_multi, "accounts_for", lambda a: [aws_accounts.Account("kestrel-prod", lambda r: Sess("kestrel-prod", r))])
    out, tso = tmp_path / "aws.json", tmp_path / "ts.json"
    aws_multi.main(["--regions", "eu-west-1,us-east-1,eu-north-1", "--out", str(out), "--ts-out", str(tso)])
    aws = json.loads(out.read_text())
    assert aws["regions"] == ["eu-west-1", "us-east-1"] and sorted(q["n"] for q in aws["sqs"]) == ["eu-west-1:orders", "us-east-1:orders"]
    assert aws["skippedScopes"] == [{"account": "kestrel-prod", "region": "eu-north-1", "why": "nothing found"}]
    assert json.loads(tso.read_text())["labels"]["queues"][0].endswith(":orders")


def test_nothing_readable_is_a_clear_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(aws_multi, "accounts_for", lambda a: [])
    try:
        aws_multi.main(["--regions", "eu-west-1,us-east-1", "--out", str(tmp_path / "a.json"), "--ts-out", str(tmp_path / "t.json")])
    except SystemExit as e:
        assert "sign" in str(e)
    else:
        raise AssertionError("expected SystemExit")
