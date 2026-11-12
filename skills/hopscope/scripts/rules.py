"""Turn the inventory, metrics, code map and links into the console model: per-resource health,
a prioritised action queue, savings, spend and an overall status. Writes model.json.

    python rules.py --data data/ [--overrides hopscope.json]

Thresholds live in THRESHOLDS and can be overridden under "thresholds" in hopscope.json. Cost figures are
rough list-price estimates, labelled "≈", meant to rank actions rather than replace a bill.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import drift
import history
import rules_streams

THRESHOLDS = {"err_pct_critical": 5.0, "mem_critical": 85.0, "mem_warn": 75.0, "cpu_warn": 85.0, "db_pinned_hours": 12, "db_cpu_critical": 90.0,
              "single_ec2_share": 0.25, "queue_age_warn_s": 3600, "ecr_repos_warn": 200}
EBS_GB_MONTH, IDLE_ALB_MONTH, EIP_MONTH = 0.09, 18.0, 3.6


def fmt_n(v: float) -> str:
    return f"{round(v):,}"


def db_size(d: dict) -> str:
    """How a database is sized, without printing missing numbers (design rows have none)."""
    if d["serverless"]:
        if d.get("minAcu") is not None and d.get("maxAcu") is not None:
            return f'Aurora serverless {d["minAcu"]}–{d["maxAcu"]} ACU'
        return "Aurora serverless"
    return d["class"] or d.get("engine") or "database"


def infra_entries(inv: dict, links: dict) -> tuple[list[dict], dict[str, str]]:
    """Every compute and database resource as one row the console can draw."""
    placed = {k: v["code"] for k, v in links["placement"].items()}
    labels = dict(links["labels"])
    design = inv.get("mode") == "design"
    rows: list[dict] = []
    counts: dict[str, int] = {}
    for x in inv["ecs"]:
        counts[x["n"]] = counts.get(x["n"], 0) + 1
    for x in inv["ecs"]:
        key = f'{x["cluster"]}/{x["n"]}'
        sys_id = placed.get(key) or f"cluster:{x['cluster']}"
        labels.setdefault(sys_id, x["cluster"] if sys_id.startswith("cluster:") else sys_id)
        # Same service name in two clusters (prod and staging copies): keep prod's plain name.
        name = x["n"] if counts[x["n"]] == 1 or x["env"] == "prod" else f'{x["n"]} ({x["cluster"]})'
        rows.append({"n": name, "key": key, "sys": sys_id, "kind": "ecs", "env": x["env"], "cpu": round(x["cpu"], 2) if x["running"] else 0,
                     "mem": round(x["mem"], 1) if x["running"] and not design else None, "memMax": x["memMax"], "size": x["size"] or (f'planned in {x.get("source") or "files"}' if design else "Fargate"),
                     "cluster": x["cluster"], "tasks": x["running"], **({"src": "kubernetes", "workload": x.get("workload"), "hpa": x.get("hpa")} if x.get("source") == "kubernetes" else {})})
    for e in inv["ec2"]:
        if e["state"] != "running":
            continue
        rows.append({"n": e["n"], "sys": "servers" if e["env"] == "prod" else "staging", "kind": "ec2", "env": e["env"], "cpu": round(e["cpu"], 1),
                     "mem": None, "size": e["type"], "credits": e["credits"], "creditMin": e["creditMin"], "surplus": e["surplus"]})
    labels.setdefault("servers", "EC2 servers")
    labels.setdefault("staging", "Staging and dev servers")
    for d in inv["rds"]:
        cap = (100 * d["acu"] / d["maxAcu"]) if d["serverless"] and d["maxAcu"] else d["cpu"]
        rows.append({"n": d["n"], "sys": "data", "kind": "rds", "env": d["env"], "cpu": round(cap, 1), "mem": None, "conns": d["conns"],
                     "size": db_size(d)})
    labels.setdefault("data", "Databases")
    return rows, labels


def flag_key(rows: list[dict], key: str, lvl: str, note: str) -> None:
    for r in rows:
        if r.get("key") == key:
            r["lvl"], r["note"] = lvl, note


def flag(rows: list[dict], name: str, lvl: str, note: str) -> None:
    for r in rows:
        if r["n"] == name and (r.get("lvl") != "bad"):
            r["lvl"], r["note"] = lvl, note


def evaluate(inv: dict, ts: dict, links: dict, rows: list[dict], th: dict) -> tuple[list[list], list[list]]:
    actions: list[list] = []
    savings: list[list] = []
    running = {f'{x["cluster"]}/{x["n"]}': x["running"] for x in inv["ecs"]}
    # Load balancers: error share and routes that lead nowhere.
    for lb in inv["lb"]:
        k8s = lb.get("source") == "kubernetes"  # Kubernetes services carry no load balancer metrics: only dead routes are checked
        dead = [r for r in lb["routes"] if (r["services"] and all(running.get(s, 0) == 0 for s in r["services"])) or (not r["services"] and r["targets"] and not any(t == "healthy" for t in r["targets"])) or (not r["services"] and not r["targets"])]
        for r in dead:
            for s in r["services"]:
                flag_key(rows, s, "bad", f'0 tasks, but {lb["n"]} routes {r["when"]} here')
        if k8s:
            continue
        if lb["type"] == "application" and lb["req"] > 0:
            err = lb["err"] / lb["req"] * 100
            if err >= th["err_pct_critical"]:
                names = ", ".join(sorted({r["tg"] for r in dead})) or "unknown targets"
                actions.append(["bad", f'{fmt_n(lb["err"])} requests failed at {lb["n"]} in 3 days', f'{err:.0f}% of its traffic ({fmt_n(lb["e503"])} were 503 "no healthy target"). Rules with nothing healthy behind them: {names}.',
                                "Start the missing services or delete those rules, then turn on load balancer access logs to see who is calling."])
            if lb["req"] < 1:
                savings.append([f'Remove idle load balancer {lb["n"]}', f"≈ ${IDLE_ALB_MONTH:.0f}"])
        elif lb["req"] == 0:
            savings.append([f'Remove idle load balancer {lb["n"]}', f"≈ ${IDLE_ALB_MONTH:.0f}"])
        if lb["type"] == "network" and lb["err"] > max(100, lb["req"]):
            actions.append(["warn", f'{lb["n"]}: {fmt_n(lb["err"])} target resets vs {fmt_n(lb["req"])} connections', "Something behind it keeps refusing connections.", "Check the target health checks and the listener ports."])
    # Database saturation.
    s = ts.get("series", {})
    lab = ts.get("labels", {})
    if lab.get("db") and s.get("dbcap"):
        if lab["dbKind"] == "acu" and lab.get("dbMax"):
            pinned = sum(1 for v in s["dbcap"] if v >= lab["dbMax"] - 0.01)
            if pinned >= th["db_pinned_hours"]:
                flag(rows, lab["db"], "bad", f'At its {lab["dbMax"]} ACU limit in {pinned} of 72 hours')
                actions.append(["bad", f'Database {lab["db"]} is capped at its limit', f'It hit its {lab["dbMax"]} ACU maximum in {pinned} of the last 72 hours; queries slow down at the ceiling.',
                                'Raise the maximum capacity (billed only when used), then look at the heaviest queries.'])
        elif max(s["dbcap"]) >= th["db_cpu_critical"]:
            hours = sum(1 for v in s["dbcap"] if v >= th["db_cpu_critical"])
            flag(rows, lab["db"], "bad", f"CPU above {th['db_cpu_critical']:.0f}% in {hours} hours")
            actions.append(["bad", f'Database {lab["db"]} is running hot', f"CPU above {th['db_cpu_critical']:.0f}% in {hours} of 72 hours.", "Find the heaviest queries or move to a larger instance."])
    for d in inv["rds"]:
        if d["env"] != "prod" and d["serverless"] and (d["minAcu"] or 0) > 0:
            flag(rows, d["n"], "warn", f'Never pauses (minimum {d["minAcu"]} ACU)')
            savings.append([f'Let staging database {d["n"]} pause (minimum {d["minAcu"]} → 0 ACU)', "≈ $30–45"])
        if d["conns"] <= 1 and d["acu"] < 0.2 and d.get("source") != "kubernetes":
            flag(rows, d["n"], "warn", f'{d["conns"]:.0f} connection in 3 days')
    # Containers.
    for x in inv["ecs"]:
        if not x["running"]:
            continue
        key = f'{x["cluster"]}/{x["n"]}'
        if x["memMax"] >= th["mem_critical"]:
            flag_key(rows, key, "bad", f'Memory peaked at {x["memMax"]:.0f}%')
            actions.append(["bad", f'{x["n"]} memory peaked at {x["memMax"]:.0f}%', f'It has {x["size"] or "its current size"}; it will restart under load.', "Raise the task memory one step."])
        elif x["memMax"] >= th["mem_warn"]:
            flag_key(rows, key, "warn", f'Memory peaked at {x["memMax"]:.0f}%')
        if x["cpuMax"] >= th["cpu_warn"] and x["cpu"] > 40:
            flag_key(rows, key, "warn", f'CPU often high (avg {x["cpu"]:.0f}%)')
        if x["env"] != "prod" and x["cpu"] < 1 and (x["mem"] or 0) < 30 and x.get("source") != "kubernetes":
            flag_key(rows, key, "warn", "Staging, idle. Scale to zero when not testing")
    singles = [x for x in inv["ecs"] if x["env"] == "prod" and x["running"] == 1 and not x.get("hpa") and x.get("workload") not in ("CronJob", "Job", "DaemonSet")]
    if singles and len(singles) >= 0.5 * max(1, sum(1 for x in inv["ecs"] if x["env"] == "prod" and x["running"])):
        actions.append(["warn", f"{len(singles)} production services run a single task", "Any restart or deploy is a short outage for that service.",
                        "Run at least 2 tasks for the services on the main request path."])
    # Queues.
    for q in inv["sqs"]:
        if q["dlq"] and q["backlog"] > 0:
            actions.append(["bad", f'{q["backlog"]:.0f} failed message(s) in {q["n"]}', f'The oldest has waited {q["age"] / 3600:.0f} hours.', "Inspect them, fix the cause, then redrive."])
            rows.append({"n": q["n"], "sys": "data", "kind": "sqs", "env": "staging" if re.search(r"stag|dev|test", q["n"]) else "prod", "cpu": 0, "size": "SQS dead-letter queue",
                         "lvl": "bad", "note": f'{q["backlog"]:.0f} failed message(s), oldest {q["age"] / 3600:.0f} h'})
        elif not q["dlq"] and q["backlog"] > 0 and q["age"] > th["queue_age_warn_s"]:
            actions.append(["warn", f'{q["n"]} is backing up', f'{q["backlog"]:.0f} waiting, oldest {q["age"] / 60:.0f} min.', "Check that its worker is running and keeping up."])
    # Servers.
    total = sum(x["req"] for x in ts.get("byService", []))
    tg_host = links.get("tgHost", {})
    for x in ts.get("byService", []):
        if total and x["tg"] in tg_host and x["req"] / total >= th["single_ec2_share"] and len(x.get("targets", [])) == 1:
            actions.append(["warn", f'{tg_host[x["tg"]]} is a single EC2 carrying {x["req"] / total * 100:.0f}% of traffic', f'{fmt_n(x["req"])} requests in 3 days through one server.', "Add a second instance behind its target group, or move it to containers."])
    for e in inv["ec2"]:
        if e["state"] == "stopped" and e["diskGB"]:
            savings.append([f'Snapshot and delete the {e["diskGB"]} GB disk of stopped server {e["n"]}', f'≈ ${e["diskGB"] * EBS_GB_MONTH:.0f}'])
        elif e["state"] == "running":
            if e["credits"] == "unlimited" and (e["creditMin"] or 0) == 0 and e["surplus"] > 0:
                flag(rows, e["n"], "warn", "Out of CPU credits, paying surplus")
            if e["env"] != "prod":
                flag(rows, e["n"], "warn", "Staging/dev server running 24×7")
                savings.append([f'Stop {e["n"]} ({e["type"]}) outside working hours', "≈ two-thirds of its cost"])
            elif e["cpuMax"] < 15 and e["cpu"] < 3:
                flag(rows, e["n"], "warn", f'Nearly idle ({e["cpu"]:.1f}% CPU)')
    eips = inv.get("eips") or {}
    if eips.get("unattached"):
        savings.append([f'Release {eips["unattached"]} unattached public IP address(es)', f'≈ ${eips["unattached"] * EIP_MONTH:.0f}'])
    if (inv.get("ecr") or 0) >= th["ecr_repos_warn"]:
        actions.append(["warn", f'{inv["ecr"]:,} container image repositories', "Old images pile up and are billed as storage.", "Add a lifecycle rule that expires old images."])
    order = {"bad": 0, "warn": 1}
    actions.sort(key=lambda a: order.get(a[0], 2))
    return actions, savings


def evaluate_design(inv: dict, links: dict) -> list[list]:
    """Checks that need no metrics: what the files say is planned."""
    actions: list[list] = []
    orphans = [f'{lb["n"]}: {r["when"]}' for lb in inv["lb"] for r in lb["routes"]
               if not r["services"] and not r.get("attached") and r.get("tgType", "ip") not in ("lambda", "alb", "instance", "external")]
    if orphans:  # the target group may be served from elsewhere, so this is a question, not a fault
        actions.append(["warn", f"{len(orphans)} route(s) have no service for them in these files",
                        f'{"; ".join(orphans[:5])}. The target group may be served from another folder, module or account.',
                        "Point Hopscope at the repo that defines it, or ignore."])
    no_dlq = [q["n"] for q in inv["sqs"] if not q["dlq"] and not q.get("redrive")]
    if no_dlq:
        actions.append(["warn", f"{len(no_dlq)} queue(s) have no dead-letter queue", f'{", ".join(no_dlq[:5])}: a message that keeps failing is retried forever or lost.',
                        "Add a dead-letter queue with a redrive policy."])
    singles = [x["n"] for x in inv["ecs"] if x["env"] == "prod" and x["desired"] == 1 and x.get("ownImage") is not False and x.get("source") != "compose"
               and not x.get("hpa") and x.get("workload") not in ("CronJob", "Job", "DaemonSet")]  # jobs run to completion; a DaemonSet runs on every node
    if singles:
        actions.append(["warn", f"{len(singles)} production service(s) planned with a single task", f'{", ".join(singles[:5])}: any restart or deploy is a short outage.',
                        "Set the desired count to at least 2 for services on the request path."])
    why = links.get("unplacedWhy") or {}
    no_code = [k for k in links.get("unplaced") or [] if k in why]
    unknown = [k for k in links.get("unplaced") or [] if k not in why]
    if unknown:
        actions.append(["warn", f"{len(unknown)} service(s) with no code found", ", ".join(unknown[:5]),
                        'Point Hopscope at the repo that builds them, or set them in hopscope.json under "placement".'])
    if no_code:
        actions.append(["warn", f"{len(no_code)} service(s) build a folder with no code scanned", "; ".join(f"{k} {why[k]}" for k in no_code[:5]),
                        'Fine for data or config jobs; if one runs code, set it in hopscope.json under "placement".'])
    actions.sort(key=lambda a: {"bad": 0, "warn": 1}.get(a[0], 2))
    return actions


def extra_actions(d: Path, inv: dict, ts: dict, design: bool, th: dict | None = None) -> tuple[list[list], list[list]]:
    """(deploy and stream actions, drift actions): a deploy right before an error jump, services whose tasks failed to start (at most
    three), failed Step Functions executions, late Kinesis readers and failing event source mappings, and at most three items from
    the drift report."""
    first: list[list] = []
    last: list[list] = []
    try:
        if not design:
            first = history.deploy_action(inv, ts) + history.failure_actions(inv) + rules_streams.actions(inv, th or THRESHOLDS)
        rep = json.loads((d / "drift.json").read_text()) if (d / "drift.json").exists() else {}
        last = drift.actions(rep)
    except (ValueError, KeyError, TypeError) as e:  # a malformed extra file must not stop the model
        print(f"warning: drift and deploy actions skipped ({type(e).__name__})")
    return first, last


def examples(code: dict, flows: dict, placed: set[str]) -> list[str]:
    """Five questions worth asking: the routes with the richest real call sequences."""
    scored = []
    for i, r in enumerate(code["routes"]):
        steps = flows.get(str(i), [])
        ext = {s[2].split(":")[0] for s in steps if s[1] != "call"}
        svc = next((s for s in sorted(code["services"], key=len, reverse=True) if code["files"][r["f"]]["p"].startswith(s + "/")), None)
        if ext and svc in placed:
            scored.append((len(ext) * 10 + len(steps), r))
    picked, seen = [], set()
    for _, r in sorted(scored, key=lambda x: -x[0]):
        if r["p"] in seen:
            continue
        seen.add(r["p"])
        picked.append(f'Show me {r["m"]} {r["p"]}')
        if len(picked) == 5:
            break
    return picked


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", required=True)
    ap.add_argument("--overrides")
    a = ap.parse_args()
    d = Path(a.data)
    inv, code, links = (json.loads((d / f).read_text()) for f in ("aws.json", "code.json", "links.json"))
    ts = json.loads((d / "ts.json").read_text()) if (d / "ts.json").exists() and inv.get("mode") != "design" else {}  # a stale ts.json is not about these files
    flows = json.loads((d / "flows.json").read_text()) if (d / "flows.json").exists() else {}
    ov = json.loads(Path(a.overrides).read_text()) if a.overrides and Path(a.overrides).exists() else {}
    th = {**THRESHOLDS, **(ov.get("thresholds") or {})}
    rows, labels = infra_entries(inv, links)
    design = inv.get("mode") == "design"
    actions, savings = (evaluate_design(inv, links), []) if design else evaluate(inv, ts, links, rows, th)
    first, last = extra_actions(d, inv, ts, design, th)
    actions = first + actions + last  # a deploy right before errors is the lead worth reading first among the warnings
    actions.sort(key=lambda a: {"bad": 0, "warn": 1}.get(a[0], 2))
    crit = [x for x in actions if x[0] == "bad"]
    status = {"state": "bad" if crit else ("warn" if actions else "ok"),
              "headline": " · ".join(x[1] for x in crit[:2]) if crit else (("Design view: nothing critical in the files" if design else "No critical issues") if not actions else actions[0][1]),
              "detail": f"{len(crit)} critical · {sum(1 for x in actions if x[0] == 'warn')} warnings" + ("" if design else f" · {len(savings)} savings found")}
    by_key = {row["key"]: row for row in rows if row.get("key")}
    placement: dict[str, dict] = {}
    for key, p in sorted(links["placement"].items(), key=lambda kv: (by_key.get(kv[0], {}).get("env") != "prod", -kv[1]["confidence"])):
        if key in by_key:  # production copies first, so traces land on prod
            placement.setdefault(p["code"], {"sys": p["code"], "compute": []})["compute"].append(by_key[key]["n"])
    model = {"meta": {"mode": inv.get("mode", "live"), "region": inv["region"], "account": inv.get("account"), "collectedAt": inv["collectedAt"], "codeSource": code.get("source"),
                      "company": ov.get("company") or ""},
             "infra": rows, "systems": labels, "placement": placement, "queues": links["queues"], "queueWorkers": links["queueWorkers"],
             "tgHost": links["tgHost"], "dbFor": links["dbFor"], "streams": links.get("streams") or {}, "consumers": links.get("consumers") or {}, "primaryDb": (ts.get("labels") or {}).get("db"),
             "actions": actions, "savings": savings, "spend": inv.get("cost", [])[:12], "status": status,
             "examples": ov.get("examples") or examples(code, flows, set(links["codeToCompute"])), "review": links["review"], "unplaced": links["unplaced"]}
    (d / "model.json").write_text(json.dumps(model, separators=(",", ":")))
    print(f"model: {len(rows)} resources, {len(crit)} critical, {len(actions) - len(crit)} warnings, {len(savings)} savings -> {d / 'model.json'}")


if __name__ == "__main__":
    main()
