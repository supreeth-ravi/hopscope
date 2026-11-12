"""aws_inventory: ECS deploy history (deployments, revisions, events) from stubbed boto3 responses. No AWS account is used."""
import json
from datetime import datetime, timedelta, timezone

import aws_inventory

ACCT = "0" * 12  # built at runtime so no account-shaped literal sits in the source
REGION = "us-east-1"
T0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)


def td_arn(fam: str, rev: int) -> str:
    return f"arn:aws:ecs:{REGION}:{ACCT}:task-definition/{fam}:{rev}"


class Pager:
    def __init__(self, pages: dict):
        self.pages = pages

    def paginate(self, **kw):
        yield self.pages


class FakeECS:
    def __init__(self, list_fails: bool = False):
        self.list_fails = list_fails
        self.described: list[str] = []

    def get_paginator(self, op):
        return Pager({"list_clusters": {"clusterArns": [f"arn:aws:ecs:{REGION}:{ACCT}:cluster/prod"]},
                      "list_services": {"serviceArns": [f"arn:aws:ecs:{REGION}:{ACCT}:service/prod/api"]}}[op])

    def describe_services(self, cluster, services):
        tg = f"arn:aws:elasticloadbalancing:{REGION}:{ACCT}:targetgroup/api-tg/abc123def456"
        return {"services": [{
            "serviceName": "api", "runningCount": 2, "desiredCount": 2, "taskDefinition": td_arn("shop-api", 43), "loadBalancers": [],
            "deployments": [{"status": "PRIMARY", "taskDefinition": td_arn("shop-api", 43), "createdAt": T0, "updatedAt": T0 + timedelta(minutes=9),
                             "rolloutState": "COMPLETED", "desiredCount": 2, "runningCount": 2}],
            "events": [{"createdAt": T0 + timedelta(minutes=10), "message": "(service api) has reached a steady state."},
                       {"createdAt": T0 + timedelta(minutes=2), "message": f"(service api) registered 1 targets in (target-group {tg})"}]
                      + [{"createdAt": T0 - timedelta(hours=i), "message": f"(service api) event {i}"} for i in range(1, 40)],
        }]}

    def describe_task_definition(self, taskDefinition):
        self.described.append(taskDefinition)
        rev = int(taskDefinition.rsplit(":", 1)[1])
        return {"taskDefinition": {"family": "shop-api", "revision": rev, "registeredAt": T0 - timedelta(hours=43 - rev), "cpu": "1024", "memory": "2048",
                                   "containerDefinitions": [{"name": "api", "image": f"{ACCT}.dkr.ecr.{REGION}.amazonaws.com/shop-api:v{rev}"}]}}

    def list_task_definitions(self, familyPrefix, sort, maxResults):
        if self.list_fails:
            raise PermissionError("AccessDenied")
        # familyPrefix also matches longer family names; those must be ignored
        return {"taskDefinitionArns": [td_arn("shop-api-admin", 9)] + [td_arn("shop-api", r) for r in range(43, 35, -1)]}


class FakeCW:
    def get_metric_data(self, **kw):
        return {"MetricDataResults": []}


class FakeSession:
    def __init__(self, ecs):
        self.ecs = ecs

    def client(self, name, **kw):
        return self.ecs


def test_ecs_rows_carry_task_definition_and_deploys() -> None:
    ecs = FakeECS()
    rows = aws_inventory.collect_ecs(FakeSession(ecs), FakeCW(), None)
    x = rows[0]
    assert x["taskDef"] == "shop-api:43" and x["taskDefAt"] == "2026-11-01T08:00:00+00:00"
    kinds = [d["kind"] for d in x["deploys"]]
    assert kinds.count("deployment") == 1 and kinds.count("event") == aws_inventory.MAX_EVENTS
    revs = [d["taskDef"] for d in x["deploys"] if d["kind"] == "revision"]
    assert revs == ["shop-api:43", "shop-api:42", "shop-api:41", "shop-api:40", "shop-api:39"]  # capped at 5, other family ignored
    dep = next(d for d in x["deploys"] if d["kind"] == "deployment")
    assert dep == {"kind": "deployment", "at": "2026-11-01T08:00:00+00:00", "updated": "2026-11-01T08:09:00+00:00", "status": "PRIMARY",
                   "rollout": "COMPLETED", "taskDef": "shop-api:43", "desired": 2, "running": 2}
    ats = [d["at"] for d in x["deploys"]]
    assert ats == sorted(ats, reverse=True)
    # existing fields are unchanged
    assert x["images"] == ["shop-api"] and x["running"] == 2 and x["ownImage"] is True


def test_nothing_account_specific_is_kept() -> None:
    rows = aws_inventory.collect_ecs(FakeSession(FakeECS()), FakeCW(), None)
    text = json.dumps(rows)
    assert ACCT not in text and "arn:aws" not in text
    msgs = [d["msg"] for d in rows[0]["deploys"] if d["kind"] == "event"]
    assert "(service api) registered 1 targets in (target-group api-tg)" in msgs


def test_missing_list_permission_keeps_the_current_revision(capsys) -> None:
    rows = aws_inventory.collect_ecs(FakeSession(FakeECS(list_fails=True)), FakeCW(), None)
    assert [d["taskDef"] for d in rows[0]["deploys"] if d["kind"] == "revision"] == ["shop-api:43"]
    assert "earlier task-definition revisions skipped" in capsys.readouterr().out


def test_mask_text_and_arn_names() -> None:
    lb = f"arn:aws:elasticloadbalancing:{REGION}:{ACCT}:loadbalancer/app/public/0123abcd"
    assert aws_inventory.mask_text(f"in {lb} for account {ACCT}") == "in public for account ************"
    assert aws_inventory.arn_name(f"arn:aws:ecs:{REGION}:{ACCT}:service/prod/api") == "api"
    assert len(aws_inventory.mask_text("x" * 1000)) == 300
    assert aws_inventory.iso(None) == ""


def test_numbers_glued_to_text_are_masked_too() -> None:
    assert aws_inventory.mask_text(f"task-{ACCT}x") == "task-************x"
    svc = {"deployments": [{"taskDefinition": f"fam{ACCT}:3", "createdAt": T0}], "events": [{"createdAt": T0, "message": f"id {ACCT}"}]}
    assert ACCT not in json.dumps(aws_inventory.deploys_of(svc, [(f"fam{ACCT}:3", T0)]))


def test_steady_state_events_are_counted_not_kept() -> None:
    svc = {"deployments": [], "events": [{"createdAt": T0 - timedelta(hours=i), "message": "(service api) has reached a steady state."} for i in range(60)]
           + [{"createdAt": T0 - timedelta(minutes=i), "message": f"(service api) failed container health checks {i}"} for i in range(30)]}
    out = aws_inventory.deploys_of(svc, [])
    msgs = [d["msg"] for d in out if d["kind"] == "event"]
    assert len(msgs) == aws_inventory.MAX_EVENTS and not any("steady state" in m for m in msgs)
    assert aws_inventory.steady_events(svc) == 60
    rows = aws_inventory.collect_ecs(FakeSession(FakeECS()), FakeCW(), None)
    assert rows[0]["steadyEvents"] == 1 and not any("steady state" in d.get("msg", "") for d in rows[0]["deploys"])


def test_event_text_hides_addresses_hosts_and_endpoints() -> None:
    msg = ("(service api) (instance i-0abc) (port 8080) is unhealthy in target-group api due to (reason Health checks failed) at 10.0.3.44, "
           "host ip-10-0-3-44.ec2.internal, db app-db.k3m9q2w7z1ab.eu-west-1.rds.amazonaws.com")
    out = aws_inventory.mask_text(msg)
    assert "10.0.3.44" not in out and "<ip>" in out
    assert "ip-10-0-3-44" not in out and "<host>" in out
    assert "k3m9q2w7z1ab" not in out and "app-db.xxxxxxxx.eu-west-1.rds.amazonaws.com" in out
