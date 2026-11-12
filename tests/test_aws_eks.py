"""EKS and live Kubernetes (aws_eks.py): stubbed boto3 and stubbed kubectl output only; no cluster or AWS account is used."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

import aws_eks
import history

ACCT = "0" * 11 + "7"  # built at runtime so no account-shaped literal sits in the source
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


class Pager:
    def __init__(self, key: str, items: list):
        self.key, self.items = key, items

    def paginate(self, **kw):
        yield {self.key: self.items}


class FakeEKS:
    def get_paginator(self, op):
        return {"list_clusters": Pager("clusters", ["kestrel-prod"]), "list_nodegroups": Pager("nodegroups", ["general"])}[op]

    def describe_cluster(self, name):
        return {"cluster": {"name": name, "version": "1.31", "status": "ACTIVE", "arn": f"arn:aws:eks:eu-west-1:{ACCT}:cluster/{name}",
                            "endpoint": "https://ABCDEF0123456789.gr7.eu-west-1.eks.amazonaws.com"}}

    def describe_nodegroup(self, clusterName, nodegroupName):
        return {"nodegroup": {"nodegroupName": nodegroupName, "status": "ACTIVE", "instanceTypes": ["m6i.large"], "capacityType": "ON_DEMAND",
                              "scalingConfig": {"minSize": 2, "maxSize": 6, "desiredSize": 3}, "nodeRole": f"arn:aws:iam::{ACCT}:role/ng"}}


class Session:
    def client(self, name, **kw):
        assert name == "eks"
        return FakeEKS()


def test_eks_clusters_and_nodegroups_without_account_details() -> None:
    rows = aws_eks.collect_eks(Session())
    assert rows == [{"n": "kestrel-prod", "version": "1.31", "status": "ACTIVE",
                     "nodegroups": [{"n": "general", "status": "ACTIVE", "types": ["m6i.large"], "capacity": "ON_DEMAND", "min": 2, "max": 6, "desired": 3}]}]
    assert ACCT not in json.dumps(rows) and "ABCDEF" not in json.dumps(rows)


def _ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


WORKLOADS = {"items": [
    {"kind": "Deployment", "metadata": {"name": "brew-api", "namespace": "kestrel"},
     "spec": {"replicas": 3, "selector": {"matchLabels": {"app": "brew-api"}},
              "template": {"metadata": {"labels": {"app": "brew-api"}},
                           "spec": {"containers": [{"name": "api", "image": f"{ACCT}.dkr.ecr.eu-west-1.amazonaws.com/brew-api:9",
                                                    "env": [{"name": "ORDERS_QUEUE_URL", "value": f"https://sqs.eu-west-1.amazonaws.com/{ACCT}/kestrel-orders"},
                                                            {"name": "STRIPE_KEY", "value": "sk_live_abcdef"}]}]}}},
     "status": {"readyReplicas": 2}},
    {"kind": "Service", "metadata": {"name": "brew-api", "namespace": "kestrel"}, "spec": {"selector": {"app": "brew-api"}, "ports": [{"port": 80}]}},
]}
EVENTS = {"items": [
    {"type": "Warning", "reason": "BackOff", "message": "Back-off restarting failed container api in pod brew-api-7d9f8c6b5-x2x9z", "count": 14,
     "involvedObject": {"kind": "Pod", "name": "brew-api-7d9f8c6b5-x2x9z", "namespace": "kestrel"}, "lastTimestamp": _ts(2)},
    {"type": "Warning", "reason": "Failed", "message": f"Failed to pull image \"{ACCT}.dkr.ecr.eu-west-1.amazonaws.com/brew-api:10\": not found; node ip-10-0-1-5.eu-west-1.compute.internal 10.0.1.5",
     "involvedObject": {"kind": "Pod", "name": "brew-api-7d9f8c6b5-abcde", "namespace": "kestrel"}, "lastTimestamp": _ts(1)},
    {"type": "Warning", "reason": "FailedScheduling", "message": "0/3 nodes are available: 3 Insufficient memory.",
     "involvedObject": {"kind": "Pod", "name": "brew-api-7d9f8c6b5-qqqqq", "namespace": "kestrel"}, "eventTime": _ts(3)},
    {"type": "Warning", "reason": "OOMKilling", "message": "Memory cgroup out of memory: Killed process 4242 (python)",
     "involvedObject": {"kind": "Pod", "name": "brew-api-7d9f8c6b5-x2x9z", "namespace": "kestrel"}, "lastTimestamp": _ts(5)},
    {"type": "Warning", "reason": "BackOff", "message": "old", "involvedObject": {"kind": "Pod", "name": "brew-api-1-2", "namespace": "kestrel"}, "lastTimestamp": _ts(100)},
    {"type": "Normal", "reason": "Pulled", "message": "ok", "involvedObject": {"kind": "Pod", "name": "brew-api-1-2", "namespace": "kestrel"}, "lastTimestamp": _ts(1)},
    {"type": "Warning", "reason": "BackOff", "message": "other ns", "involvedObject": {"kind": "Pod", "name": "brew-api-1-2", "namespace": "other"}, "lastTimestamp": _ts(1)},
]}


@pytest.fixture()
def kubectl(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(cmd, **kw):
        calls.append(cmd)
        assert kw.get("shell") in (None, False) and kw.get("timeout") and isinstance(cmd, list)
        body = EVENTS if "events" in cmd else WORKLOADS
        kw["stdout"].write(json.dumps(body).encode())  # kubectl writes to a temporary file, never to memory
        return subprocess.CompletedProcess(cmd, 0, stdout=None, stderr=b"")

    monkeypatch.setattr(aws_eks.shutil, "which", lambda name: "/usr/local/bin/kubectl")
    monkeypatch.setattr(aws_eks.subprocess, "run", run)
    return calls


def test_kubectl_is_read_only_and_never_reads_secrets(kubectl: list[list[str]]) -> None:
    aws_eks.collect_kube(["kestrel-prod"], NOW)
    assert all(c[0] == "/usr/local/bin/kubectl" and c[1:3] == ["--context", "kestrel-prod"] and c[3] == "get" for c in kubectl)
    assert all("secret" not in " ".join(c).lower() for c in kubectl)
    assert any("deploy,sts,ds,svc,ing,hpa,cronjob" in c for c in kubectl) and any("events" in c for c in kubectl)


def test_live_workloads_use_the_design_mapper(kubectl: list[list[str]]) -> None:
    part = aws_eks.collect_kube(["kestrel-prod"], NOW)
    api = part["ecs"][0]
    assert (api["cluster"], api["n"], api["desired"], api["running"], api["source"]) == ("kestrel", "brew-api", 3, 2, "kubernetes")
    assert api["refs"] == [{"k": "ORDERS_QUEUE_URL", "v": "https://sqs.eu-west-1.amazonaws.com/************/kestrel-orders"}]
    assert part["lb"][0]["routes"][0]["services"] == ["kestrel/brew-api"]
    assert ACCT not in json.dumps(part) and "sk_live" not in json.dumps(part)


def test_warning_events_become_masked_deploy_rows_and_failure_actions(kubectl: list[list[str]]) -> None:
    part = aws_eks.collect_kube(["kestrel-prod"], NOW)
    events = part["ecs"][0]["deploys"]
    assert [e["kind"] for e in events] == ["event"] * 4
    text = json.dumps(events)
    assert "CrashLoopBackOff" in text and "ImagePullBackOff" in text and "FailedScheduling" in text and "OOMKilled" in text
    assert ACCT not in text and "10.0.1.5" not in text and "ip-10-0-1-5" not in text and "(x14)" in text
    actions = history.failure_actions({"collectedAt": NOW.isoformat(), "ecs": part["ecs"]})
    assert len(actions) == 1 and actions[0][1].startswith("brew-api couldn't start tasks 4 times")
    for reason in ("container keeps crashing", "image could not be pulled", "no capacity to place the task", "container ran out of memory"):
        assert reason in actions[0][2]


def test_several_contexts_keep_clusters_apart(kubectl: list[list[str]]) -> None:
    arn = f"arn:aws:eks:eu-west-1:{ACCT}:cluster/kestrel-staging"
    part = aws_eks.collect_kube(["kestrel-prod", arn], NOW)
    assert {r["cluster"] for r in part["ecs"]} == {"kestrel-prod:kestrel", "kestrel-staging:kestrel"}
    assert ACCT not in json.dumps(part)


def test_refuses_without_kubectl_or_with_a_bad_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aws_eks.shutil, "which", lambda name: None)
    with pytest.raises(aws_eks.KubeError, match="kubectl"):
        aws_eks.collect_kube(["kestrel-prod"], NOW)
    monkeypatch.setattr(aws_eks.shutil, "which", lambda name: "/usr/bin/kubectl")
    for bad in ("--kubeconfig=/tmp/x", "a b", "x;rm", ""):
        with pytest.raises(aws_eks.KubeError, match="context"):
            aws_eks.collect_kube([bad], NOW)


def test_kubectl_failure_and_timeout_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aws_eks.shutil, "which", lambda name: "/usr/bin/kubectl")

    def fail(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=f"error: Unauthorized for arn:aws:iam::{ACCT}:user/x")

    monkeypatch.setattr(aws_eks.subprocess, "run", fail)
    with pytest.raises(aws_eks.KubeError) as e:
        aws_eks.collect_kube(["kestrel-prod"], NOW)
    assert ACCT not in str(e.value) and "Unauthorized" in str(e.value)

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(aws_eks.subprocess, "run", slow)
    with pytest.raises(aws_eks.KubeError, match="timed out"):
        aws_eks.collect_kube(["kestrel-prod"], NOW)


def test_contexts_from_flags() -> None:
    assert aws_eks.contexts(["a", "b,c"], "d, e") == ["a", "b", "c", "d", "e"]
    assert aws_eks.contexts([], None) == []


def test_live_rules_and_charts_ignore_kubernetes_rows_without_metrics(kubectl: list[list[str]]) -> None:
    import aws_timeseries
    import rules

    part = aws_eks.collect_kube(["kestrel-staging"], NOW)
    for x in part["ecs"]:
        x["env"] = "staging"
    db = {"n": "orders-db", "instance": "orders-db", "engine": "postgres", "class": "", "serverless": False, "minAcu": None, "maxAcu": None,
          "acu": 0, "cpu": 0, "conns": 0, "env": "prod", "source": "kubernetes"}
    inv = {"lb": part["lb"], "ecs": part["ecs"], "rds": [db], "ec2": [], "sqs": [], "eips": {}, "ecr": 0}
    links = {"placement": {}, "labels": {}, "tgHost": {}}
    rows, _ = rules.infra_entries(inv, links)
    actions, savings = rules.evaluate(inv, {}, links, rows, rules.THRESHOLDS)
    assert savings == [] and not any(r.get("note") for r in rows), (savings, rows)
    assert next(r for r in rows if r["kind"] == "ecs")["src"] == "kubernetes"
    picked = aws_timeseries.pick({**inv, "lb": [{**lb, "type": "application", "scheme": "internet-facing"} for lb in inv["lb"]]})
    assert picked["lb"] is None and picked["db"] is None and picked["mem"] is None


def test_inventory_gets_eks_and_only_named_contexts(kubectl: list[list[str]]) -> None:
    import aws_inventory

    out = {"ecs": [{"cluster": "prod", "n": "api"}], "lb": [], "rds": [], "cache": [], "search": []}
    aws_eks.add_to(out, Session(), [], aws_inventory.safe)
    assert out["eks"][0]["n"] == "kestrel-prod" and kubectl == [] and len(out["ecs"]) == 1
    aws_eks.add_to(out, Session(), ["kestrel-prod"], aws_inventory.safe)
    assert [x["n"] for x in out["ecs"]] == ["api", "brew-api"] and out["lb"]


def test_a_failing_context_is_a_warning(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    import aws_inventory

    monkeypatch.setattr(aws_eks.shutil, "which", lambda name: None)
    out: dict = {"ecs": [], "lb": []}
    aws_eks.add_to(out, Session(), ["kestrel-prod"], aws_inventory.safe)
    assert out["ecs"] == [] and "kubectl is not installed" in capsys.readouterr().out


def test_event_messages_are_rebuilt_from_an_allowlist_never_copied(kubectl: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    pw, jwt = "hunter2-kestrel", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJrZXN0cmVsIn0.c2lnbmF0dXJl"
    pod = {"kind": "Pod", "name": "brew-api-7d9f8c6b5-x2x9z", "namespace": "kestrel"}
    events = {"items": [
        {"type": "Warning", "reason": "Unhealthy", "involvedObject": pod, "lastTimestamp": _ts(1),
         "message": f'Readiness probe failed: HTTP probe failed with statuscode: 500, body: {{"db_password": "{pw}", "token": "{jwt}"}}'},
        {"type": "Warning", "reason": "FailedScheduling", "involvedObject": pod, "lastTimestamp": _ts(2),
         "message": f"0/3 nodes are available: 2 Insufficient memory, 1 node(s) had untolerated taint {{team: {pw}}}. preemption: 0/3 nodes"},
        {"type": "Warning", "reason": "Failed", "involvedObject": pod, "lastTimestamp": _ts(3),
         "message": f'Failed to pull image "registry.example/kestrel/brew-api:9?token={pw}": rpc error: {pw}'},
        {"type": "Warning", "reason": "BackOff", "involvedObject": pod, "lastTimestamp": _ts(4), "message": f"Back-off restarting failed container api {pw}"},
        {"type": "Warning", "reason": "SomethingCustom", "involvedObject": pod, "lastTimestamp": _ts(5), "message": f"custom {pw}"},
        {"type": "Warning", "reason": "FailedMount", "involvedObject": pod, "lastTimestamp": _ts(6), "message": f"MountVolume.SetUp failed for volume secret-{pw}"},
    ]}
    real_run = aws_eks.subprocess.run

    def run(cmd, **kw):
        if "events" in cmd:
            kw["stdout"].write(json.dumps(events).encode())
            return subprocess.CompletedProcess(cmd, 0, stdout=None, stderr=b"")
        return real_run(cmd, **kw)

    monkeypatch.setattr(aws_eks.subprocess, "run", run)
    rows = aws_eks.collect_kube(["kestrel-prod"], NOW)["ecs"][0]["deploys"]
    msgs = [d["msg"] for d in rows]
    text = json.dumps(rows)
    assert pw not in text and jwt not in text and "custom" not in text and "statuscode" not in text
    assert "Unhealthy: readiness probe failed" in msgs
    assert "FailedScheduling: no node could take the pod (Insufficient memory, untolerated taint)" in msgs
    assert "ImagePullBackOff: the image could not be pulled (brew-api)" in msgs
    assert "CrashLoopBackOff: the container keeps crashing and restarting" in msgs
    assert "FailedMount: a volume could not be mounted" in msgs and len(msgs) == 5


def test_kubectl_output_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aws_eks.shutil, "which", lambda name: "/usr/bin/kubectl")
    monkeypatch.setattr(aws_eks, "MAX_STDOUT", 1000)

    def huge(cmd, **kw):
        kw["stdout"].write(b"{" + b" " * 5000 + b"}")
        return subprocess.CompletedProcess(cmd, 0, stdout=None, stderr=b"")

    monkeypatch.setattr(aws_eks.subprocess, "run", huge)
    with pytest.raises(aws_eks.KubeError, match="more than"):
        aws_eks.collect_kube(["kestrel-prod"], NOW)
