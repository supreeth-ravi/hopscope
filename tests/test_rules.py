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
