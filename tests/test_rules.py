import rules


def fixtures() -> tuple[dict, dict, dict]:
    inv = {"region": "eu-west-1", "collectedAt": "2026-11-02T00:00:00+00:00",
           "ecs": [{"cluster": "prod", "n": "api", "running": 1, "desired": 1, "cpu": 5, "mem": 40, "memMax": 91, "cpuMax": 20, "size": "0.5 vCPU · 1 GB", "env": "prod"},
                   {"cluster": "prod", "n": "mcp", "running": 0, "desired": 0, "cpu": 0, "mem": 0, "memMax": 0, "cpuMax": 0, "size": "", "env": "prod"}],
           "ec2": [{"id": "i-1", "n": "old-box", "type": "t3.small", "state": "stopped", "env": "prod", "cpu": 0, "cpuMax": 0, "credits": None, "creditMin": None, "surplus": 0, "diskGB": 100}],
           "rds": [{"n": "main", "env": "prod", "serverless": True, "minAcu": 0.5, "maxAcu": 2, "acu": 1.5, "cpu": 40, "conns": 30, "class": "db.serverless"}],
           "lb": [{"n": "public", "type": "application", "scheme": "internet-facing", "req": 1000, "err": 400, "e503": 390, "t5xx": 10,
                   "routes": [{"port": 443, "when": "path-pattern=/mcp/*", "tg": "mcp-tg", "services": ["prod/mcp"], "targets": [], "instances": []},
                              {"port": 443, "when": "default", "tg": "api-tg", "services": ["prod/api"], "targets": ["healthy"], "instances": []}]}],
           "sqs": [{"n": "jobs-dlq", "dlq": True, "backlog": 3, "age": 7200, "sent": 0}], "eips": {"unattached": 2}, "ecr": 5, "cost": [["EC2", 10.0]]}
    ts = {"labels": {"db": "main", "dbKind": "acu", "dbMax": 2}, "series": {"dbcap": [2.0] * 20 + [1.0] * 52}, "byService": []}
    links = {"placement": {"prod/api": {"code": "api", "confidence": 0.9}}, "labels": {"api": "Api"}, "tgHost": {}}
    return inv, ts, links


def test_detects_dead_route_db_limit_memory_and_dlq() -> None:
    inv, ts, links = fixtures()
    rows, _ = rules.infra_entries(inv, links)
    actions, savings = rules.evaluate(inv, ts, links, rows, rules.THRESHOLDS)
    titles = " | ".join(a[1] for a in actions)
    assert "failed at public" in titles
    assert "capped at its limit" in titles
    assert "memory peaked at 91%" in titles
    assert "failed message(s) in jobs-dlq" in titles
    mcp = next(r for r in rows if r["n"] == "mcp")
    assert mcp["lvl"] == "bad"


def test_savings_for_stopped_disk_and_ips() -> None:
    inv, ts, links = fixtures()
    rows, _ = rules.infra_entries(inv, links)
    _, savings = rules.evaluate(inv, ts, links, rows, rules.THRESHOLDS)
    text = " | ".join(s[0] for s in savings)
    assert "100 GB disk of stopped server old-box" in text
    assert "Release 2 unattached" in text


def test_critical_actions_sort_first() -> None:
    inv, ts, links = fixtures()
    rows, _ = rules.infra_entries(inv, links)
    actions, _ = rules.evaluate(inv, ts, links, rows, rules.THRESHOLDS)
    sev = [a[0] for a in actions]
    assert sev == sorted(sev, key=lambda s: {"bad": 0, "warn": 1}[s])


def design_inv() -> dict:
    ecs = lambda n, d, src="terraform": {"cluster": "prod", "n": n, "desired": d, "running": d, "env": "prod", "ownImage": True, "source": src}  # noqa: E731
    return {"mode": "design", "ecs": [ecs("api", 1), ecs("web", 2), ecs("dev-db-ui", 1, "compose")],
            "sqs": [{"n": "orders", "dlq": False, "redrive": False}, {"n": "emails", "dlq": False, "redrive": True}, {"n": "emails-dlq", "dlq": True, "redrive": False}],
            "lb": [{"n": "public", "routes": [{"when": "path-pattern=/old/*", "services": []}, {"when": "default", "services": ["prod/web"]},
                                              {"when": "path-pattern=/fn/*", "services": [], "tgType": "lambda"}, {"when": "path-pattern=/vm/*", "services": [], "attached": True}]}]}


def test_design_checks() -> None:
    actions = rules.evaluate_design(design_inv(), {"unplaced": ["prod/api"]})
    titles = [a[1] for a in actions]
    assert titles[0] == "1 route(s) have no service for them in these files" and actions[0][0] == "warn"
    assert "public: path-pattern=/old/*" in actions[0][2] and "/fn/" not in actions[0][2] and "/vm/" not in actions[0][2]
    assert not any(a[0] == "bad" for a in actions)
    assert "1 queue(s) have no dead-letter queue" in titles
    assert "1 production service(s) planned with a single task" in titles  # compose services are local dev, not counted
    assert "1 service(s) with no code found" in titles


def test_build_folders_with_no_code_get_their_own_warning() -> None:
    links = {"unplaced": ["prod/api", "compose/dbt"], "unplacedWhy": {"compose/dbt": "builds analytics, where no code was scanned"}}
    actions = {a[1]: a for a in rules.evaluate_design(design_inv(), links)}
    assert actions["1 service(s) with no code found"][2] == "prod/api"
    assert actions["1 service(s) build a folder with no code scanned"][2] == "compose/dbt builds analytics, where no code was scanned"


def test_design_rows_print_no_missing_numbers() -> None:
    inv = design_inv()
    inv["ec2"] = []
    inv["rds"] = [{"n": "main", "env": "prod", "serverless": True, "minAcu": None, "maxAcu": None, "acu": 0, "cpu": 0, "conns": 0, "class": "db.serverless", "engine": "aurora-postgresql"},
                  {"n": "pg", "env": "prod", "serverless": False, "minAcu": None, "maxAcu": None, "acu": 0, "cpu": 0, "conns": 0, "class": "", "engine": "postgres"}]
    inv["ecs"] = [{**x, "cpu": 0, "mem": 0, "memMax": 0, "size": ""} for x in inv["ecs"]]
    rows, _ = rules.infra_entries(inv, {"placement": {}, "labels": {}})
    sizes = {r["n"]: r["size"] for r in rows if r["kind"] == "rds"}
    assert sizes == {"main": "Aurora serverless", "pg": "postgres"}
    assert all("None" not in str(r.get("size")) and r.get("note") is None and r["mem"] is None for r in rows)
    assert {r["size"] for r in rows if r["kind"] == "ecs"} == {"planned in terraform", "planned in compose"}


def test_design_main_writes_mode_and_no_savings(tmp_path, monkeypatch) -> None:
    import json
    import sys
    inv = design_inv()
    inv.update({"region": "design", "collectedAt": "2026-11-12T00:00:00+00:00", "ec2": [], "rds": [], "cost": []})
    inv["ecs"] = [{**x, "cpu": 0, "mem": 0, "memMax": 0, "cpuMax": 0, "size": "", "images": []} for x in inv["ecs"]]
    links = {"placement": {}, "labels": {}, "queues": {}, "queueWorkers": {}, "tgHost": {}, "dbFor": {}, "review": [], "unplaced": [], "codeToCompute": {}}
    for name, data in (("aws.json", inv), ("code.json", {"routes": [], "files": [], "services": []}), ("links.json", links)):
        (tmp_path / name).write_text(json.dumps(data))
    (tmp_path / "ts.json").write_text(json.dumps({"labels": {"db": "stale"}}))
    monkeypatch.setattr(sys, "argv", ["rules.py", "--data", str(tmp_path)])
    rules.main()
    model = json.loads((tmp_path / "model.json").read_text())
    assert model["meta"]["mode"] == "design" and model["savings"] == [] and model["status"]["state"] == "warn"
    assert "savings" not in model["status"]["detail"] and model["primaryDb"] is None
