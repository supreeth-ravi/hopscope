"""history: snapshots of each build and the diff between consecutive ones."""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import history


def inv(**over) -> dict:
    base = {
        "collectedAt": "2026-11-01T06:00:00+00:00", "region": "us-east-1",
        "ecs": [{"cluster": "prod", "n": "api", "running": 2, "desired": 2, "size": "1 vCPU · 2 GB", "images": ["shop-api"], "tgs": ["api"],
                 "taskDef": "shop-api:41", "env": "prod"},
                {"cluster": "prod", "n": "worker", "running": 1, "desired": 1, "size": "0.25 vCPU · 0.5 GB", "images": ["worker"], "tgs": [], "env": "prod"}],
        "lb": [{"n": "public", "scheme": "internet-facing", "routes": [{"when": "default", "tg": "api"}]}],
        "sqs": [{"n": "orders.fifo", "dlq": False, "fifo": True}],
        "rds": [{"n": "db", "engine": "aurora-postgresql", "class": "db.serverless", "minAcu": 0.5, "maxAcu": 4.0}],
        "lambda": [], "ec2": [], "cache": [], "sns": [{"n": "alerts"}],
    }
    base.update(over)
    return base


def write(data: Path, aws: dict, placement: dict | None = None, actions: list | None = None) -> None:
    data.mkdir(parents=True, exist_ok=True)
    (data / "aws.json").write_text(json.dumps(aws))
    (data / "links.json").write_text(json.dumps({"placement": placement or {"prod/api": {"code": "shop-api", "confidence": 0.9}}}))
    (data / "model.json").write_text(json.dumps({"actions": actions or []}))
    (data / "code.json").write_text(json.dumps({"files": [{"p": "a.py"}], "routes": [{"m": "GET", "p": "/"}], "services": ["shop-api"]}))
    (data / "flows.json").write_text(json.dumps({"0": []}))


def test_first_run_takes_a_snapshot_and_says_so(tmp_path: Path) -> None:
    data, work = tmp_path / "data", tmp_path
    write(data, inv())
    ch = history.record(data, work)
    snaps = history.snapshots(work)
    assert [s.name for s in snaps] == ["20261101T060000Z"]
    assert sorted(p.name for p in snaps[0].iterdir()) == ["aws.json", "diff.json", "links.json", "meta.json", "model.json"]
    meta = json.loads((snaps[0] / "meta.json").read_text())
    assert meta["files"]["code.json"]["routes"] == 1 and len(meta["files"]["code.json"]["sha256"]) == 64
    assert ch["first"] is True and ch["previous"] is None
    assert json.loads((data / "changes.json").read_text())["snapshot"] == "20261101T060000Z"


def test_diff_finds_added_removed_and_changed_resources(tmp_path: Path) -> None:
    data, work = tmp_path / "data", tmp_path
    write(data, inv())
    history.record(data, work)
    new = inv(collectedAt="2026-11-02T06:00:00+00:00")
    new["ecs"][0].update(desired=3, running=3, taskDef="shop-api:42", images=["shop-api-v2"])
    new["ecs"].pop(1)
    new["sqs"].append({"n": "refunds", "dlq": False, "fifo": False})
    new["lb"][0]["routes"].append({"when": "path-pattern=/admin/*", "tg": "admin"})
    new["rds"][0]["maxAcu"] = 8.0
    write(data, new, placement={"prod/api": {"code": "storefront", "confidence": 0.5}}, actions=[["warn", "3 queues back up", "", ""]])
    ch = history.record(data, work)
    assert ch["first"] is False and ch["previous"] == "20261101T060000Z"
    assert {(a["kind"], a["name"]) for a in ch["added"]} == {("sqs", "refunds")}
    assert {(a["kind"], a["name"]) for a in ch["removed"]} == {("ecs", "prod/worker")}
    got = {(c["name"], c["field"]): (c["from"], c["to"]) for c in ch["changed"]}
    assert got[("prod/api", "desired")] == (2, 3)
    assert got[("prod/api", "taskDef")] == ("shop-api:41", "shop-api:42")
    assert got[("prod/api", "images")] == ("shop-api", "shop-api-v2")
    assert got[("db", "maxAcu")] == (4.0, 8.0)
    assert got[("public", "rules")] == ("", "+ path-pattern=/admin/* -> admin")
    assert ch["links"] == [{"service": "prod/api", "from": "shop-api", "to": "storefront", "fromConfidence": 0.9, "toConfidence": 0.5}]
    assert ch["actions"] == {"new": ["3 queues back up"], "resolved": []}
    assert "1 added" in ch["summary"] and "1 removed" in ch["summary"]
    assert len(ch["history"]) == 2 and ch["history"][0]["snapshot"] == "20261102T060000Z"


def test_actions_match_across_changing_numbers(tmp_path: Path) -> None:
    old = {"actions": [["bad", "2,310 requests failed at lb in 3 days", "", ""]]}
    new = {"actions": [["bad", "2,512 requests failed at lb in 3 days", "", ""], ["warn", "New thing", "", ""]]}
    assert history.action_changes(old, new) == {"new": ["New thing"], "resolved": []}


def test_unchanged_run_reports_nothing(tmp_path: Path) -> None:
    data, work = tmp_path / "data", tmp_path
    write(data, inv())
    history.record(data, work)
    write(data, inv(collectedAt="2026-11-01T07:00:00+00:00"))
    ch = history.record(data, work)
    assert not ch["added"] and not ch["removed"] and not ch["changed"] and not ch["links"]
    assert ch["summary"] == "no changes"
    assert ch["code"]["changed"] is False


def test_keep_prunes_oldest_snapshots(tmp_path: Path) -> None:
    data, work = tmp_path / "data", tmp_path
    for h in range(5):
        write(data, inv(collectedAt=f"2026-11-01T0{h}:00:00+00:00"))
        history.record(data, work, keep=3)
    assert [s.name for s in history.snapshots(work)] == ["20261101T020000Z", "20261101T030000Z", "20261101T040000Z"]


def test_snapshots_only_move_forward(tmp_path: Path) -> None:
    data, work = tmp_path / "data", tmp_path
    write(data, inv())
    history.record(data, work)
    history.record(data, work)  # same collection time
    write(data, inv(collectedAt="2026-10-01T00:00:00+00:00"))  # a clock behind the last snapshot
    history.record(data, work, keep=1)
    assert [s.name for s in history.snapshots(work)] == ["20261101T060002Z"]  # the one just written survives pruning


def test_mode_switch_is_a_note_not_a_diff(tmp_path: Path) -> None:
    data, work = tmp_path / "data", tmp_path
    write(data, inv())
    history.record(data, work)
    write(data, inv(collectedAt="2026-11-01T07:00:00+00:00", mode="design", ecs=[]))
    ch = history.record(data, work)
    assert ch["summary"] == "mode changed (live → design): nothing to compare" and not ch["removed"]


def test_unreadable_previous_snapshot_is_a_note(tmp_path: Path) -> None:
    data, work = tmp_path / "data", tmp_path
    write(data, inv())
    history.record(data, work)
    (history.snapshots(work)[0] / "aws.json").write_text("{broken")
    write(data, inv(collectedAt="2026-11-01T07:00:00+00:00"))
    ch = history.record(data, work)
    assert ch["summary"] == "previous snapshot unreadable (aws.json): nothing to compare" and not ch["added"]


def test_since_diffs_against_a_named_snapshot(tmp_path: Path) -> None:
    data, work = tmp_path / "data", tmp_path
    write(data, inv())
    history.record(data, work)
    mid = inv(collectedAt="2026-11-01T08:00:00+00:00")
    mid["sqs"].append({"n": "a"})
    write(data, mid)
    history.record(data, work)
    last = copy.deepcopy(mid)
    last["collectedAt"] = "2026-11-01T09:00:00+00:00"
    last["sqs"].append({"n": "b"})
    write(data, last)
    history.record(data, work)
    ch = history.since(work, "20261101T060000Z")
    assert {a["name"] for a in ch["added"]} == {"a", "b"}
    try:
        history.since(work, "nope")
    except history.NoSnapshot as e:
        assert "20261101T060000Z" in str(e)
    else:
        raise AssertionError("expected NoSnapshot")


def test_large_diffs_are_capped(tmp_path: Path) -> None:
    old = history.resources(inv())
    new = history.resources(inv(**{"lambda": [{"n": f"fn-{i}", "mem": 128} for i in range(500)]}))
    d = history.diff_resources(old, new)
    assert len(d["added"]) == history.MAX_ITEMS and d["truncated"] == 500 - history.MAX_ITEMS


def test_lines_are_readable() -> None:
    d = {"added": [{"kind": "sqs", "name": "q"}], "removed": [], "changed": [{"kind": "ecs", "name": "c/api", "field": "taskDef", "from": "a:1", "to": "a:2"}],
         "links": [{"service": "c/api", "from": "x", "to": "y", "fromConfidence": 1, "toConfidence": 0.5}], "actions": {"new": ["n"], "resolved": ["r"]}}
    text = history.lines(d)
    assert "+ sqs q" in text and "~ c/api task definition: a:1 → a:2" in text and "c/api now runs y (was x)" in " ".join(text)


def test_snapshot_time_falls_back_to_now(tmp_path: Path) -> None:
    data, work = tmp_path / "data", tmp_path
    aws = inv()
    del aws["collectedAt"]
    write(data, aws)
    history.record(data, work, at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    assert history.snapshots(work)[0].name == "20260102T030405Z"


def test_deploy_right_before_an_error_jump_is_an_action() -> None:
    errs = [3] * 72
    errs[50:54] = [300, 420, 380, 260]
    ts = {"start": "2026-10-30T06:30:00+00:00", "series": {"elb5xx": errs, "e5xx": [0] * 72}}
    aws = {"ecs": [{"cluster": "prod", "n": "shop-api", "env": "prod", "deploys": [
        {"kind": "revision", "at": "2026-11-01T08:05:00+00:00", "taskDef": "shop-api:42"},
        {"kind": "event", "at": "2026-11-01T08:10:00+00:00", "msg": "(service shop-api) has started 2 tasks"},
        {"kind": "revision", "at": "2026-10-20T08:05:00+00:00", "taskDef": "shop-api:41"}]},
        {"cluster": "prod", "n": "worker", "env": "prod", "deploys": [{"kind": "deployment", "at": "2026-11-01T12:00:00+00:00", "taskDef": "w:3"}]}]}
    assert history.error_jump(ts)[0] == 50
    sus = history.deploy_suspects(aws, ts)
    assert [(s["service"], s["taskDef"], s["minutes"]) for s in sus] == [("shop-api", "shop-api:42", 25)]
    act = history.deploy_action(aws, ts)[0]
    assert act[0] == "warn" and "shop-api:42" in act[1] and "25 min before" in act[2]
    assert history.deploy_action(aws, {"start": ts["start"], "series": {"elb5xx": [3] * 72}}) == []


def test_task_failures_in_the_last_72_hours_become_actions() -> None:
    def ev(h, msg):
        return {"kind": "event", "at": f"2026-11-0{1 + (h // 24)}T{h % 24:02d}:00:00+00:00", "msg": msg}
    aws = {"collectedAt": "2026-11-02T23:00:00+00:00", "ecs": [
        {"cluster": "prod", "n": "api", "env": "prod", "deploys": [ev(h, "(service api) was unable to place a task because no container instance met all of its requirements.") for h in range(30, 36)]
         + [{"kind": "event", "at": "2026-10-29T10:00:00+00:00", "msg": "(service api) was unable to place a task (older than 72 h)"}]},
        {"cluster": "prod", "n": "worker", "env": "prod", "deploys": [ev(40, "(service worker) (task 1) failed container health checks."),
                                                                      ev(41, "CannotPullContainerError: pull image manifest has been retried")]},
        {"cluster": "prod", "n": "quiet", "env": "prod", "deploys": [ev(40, "(service quiet) has started 1 tasks.")]}]}
    acts = history.failure_actions(aws)
    assert [a[1] for a in acts] == ["api couldn't start tasks 6 times: no capacity to place the task",
                                    "worker couldn't start tasks 2 times: container health checks failed"]
    assert acts[0][0] == "bad" and acts[1][0] == "warn" and "image could not be pulled" in acts[1][2]
    many = {"collectedAt": aws["collectedAt"], "ecs": [{**aws["ecs"][0], "n": f"s{i}"} for i in range(9)]}
    assert len(history.failure_actions(many)) == 3


def test_code_build_time_alone_is_not_a_change(tmp_path) -> None:
    a, b = tmp_path / "a" / "code.json", tmp_path / "b" / "code.json"
    for f, at in ((a, "2026-01-01T00:00:00+00:00"), (b, "2026-01-02T00:00:00+00:00")):
        f.parent.mkdir()
        f.write_text(json.dumps({"generatedAt": at, "files": [{"p": "x.py"}], "routes": []}))
    assert history.file_summary(a)["sha256"] == history.file_summary(b)["sha256"]


def test_snapshot_copies_and_diff_are_masked(tmp_path: Path) -> None:
    data, work = tmp_path / "data", tmp_path
    acct = "123456789012"
    write(data, inv(account=acct), placement={"prod/api": {"code": "shop-api", "confidence": 0.9, "evidence": [f"arn:aws:ecs:us-east-1:{acct}:service/api"]}},
          actions=[["warn", f"role {acct} is wide", "x", "y"]])
    history.record(data, work)
    later = inv(collectedAt="2026-11-02T06:00:00+00:00", account=acct)
    later["sqs"] = later["sqs"] + [{"n": f"tenant_events_{acct}.fifo", "dlq": False, "fifo": True}]
    write(data, later)
    history.record(data, work)
    for snap in history.snapshots(work):
        for f in ("aws.json", "links.json", "model.json", "diff.json"):
            text = (snap / f).read_text()
            assert acct not in text, f"{snap.name}/{f}"
            json.loads(text)
    assert history.load(history.snapshots(work)[0])["links"]["placement"]["prod/api"]["confidence"] == 0.9


def test_an_unreadable_file_is_still_masked_in_the_snapshot(tmp_path: Path) -> None:
    data, work = tmp_path / "data", tmp_path
    write(data, inv())
    (data / "links.json").write_text("not json 123456789012")
    snap = history.take(data, work)
    assert "123456789012" not in (snap / "links.json").read_text()
