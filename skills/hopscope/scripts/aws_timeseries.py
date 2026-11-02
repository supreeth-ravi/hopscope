"""Hourly series for the dashboard (last 3 days) and daily spend (last 14 days), written to ts.json.

    python aws_timeseries.py --aws data/aws.json [--profile p] --out data/ts.json

It picks what to chart from the inventory: the busiest public load balancer, the busiest production
database, the production container closest to its memory limit, and the three busiest queues.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

from awsutil import env_of, metric_hourly, session


def pick(inv: dict) -> dict:
    """Choose the entities worth charting; any of them may be None in a small account."""
    albs = [lb for lb in inv["lb"] if lb["type"] == "application"]
    public = sorted([lb for lb in albs if lb["scheme"] == "internet-facing"], key=lambda lb: -lb["req"])
    dbs = sorted([d for d in inv["rds"] if d["env"] == "prod"] or inv["rds"], key=lambda d: (-d["conns"], -d["cpu"]))
    hot = sorted([x for x in inv["ecs"] if x["env"] == "prod" and x["running"]], key=lambda x: -x["memMax"])
    live = [q for q in inv["sqs"] if not q["dlq"] and q["sent"] > 0]
    queues = sorted([q for q in live if env_of(q["n"]) == "prod"] or live, key=lambda q: -q["sent"])
    return {"lb": public[0]["n"] if public else (albs[0]["n"] if albs else None), "db": dbs[0] if dbs else None,
            "mem": hot[0] if hot else None, "queues": [q["n"] for q in queues[:3]]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--aws", required=True)
    ap.add_argument("--profile")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    inv = json.load(open(a.aws))
    s = session(a.profile, inv["region"])
    cw, elb = s.client("cloudwatch"), s.client("elbv2")
    p = pick(inv)
    Q: dict[str, tuple] = {}
    by_service: list[dict] = []
    tgs: list[dict] = []
    if p["lb"]:
        lb = elb.describe_load_balancers(Names=[p["lb"]])["LoadBalancers"][0]
        dim = lb["LoadBalancerArn"].split(":loadbalancer/")[1]
        for k, metric, stat in [("req", "RequestCount", "Sum"), ("elb5xx", "HTTPCode_ELB_5XX_Count", "Sum"), ("elb503", "HTTPCode_ELB_503_Count", "Sum"),
                                ("e5xx", "HTTPCode_Target_5XX_Count", "Sum"), ("p95", "TargetResponseTime", "p95"), ("p50", "TargetResponseTime", "p50")]:
            Q[k] = ("AWS/ApplicationELB", metric, {"LoadBalancer": dim}, stat)
        tgs = elb.describe_target_groups(LoadBalancerArn=lb["LoadBalancerArn"])["TargetGroups"]
        for i, tg in enumerate(tgs):
            Q[f"tg{i}"] = ("AWS/ApplicationELB", "RequestCount", {"LoadBalancer": dim, "TargetGroup": tg["TargetGroupArn"].split(":")[-1]}, "Sum")
    db = p["db"]
    if db:
        d = {"DBInstanceIdentifier": db["instance"]}
        if db["serverless"]:
            Q["dbcap"], Q["dbavg"] = ("AWS/RDS", "ServerlessDatabaseCapacity", d, "Maximum"), ("AWS/RDS", "ServerlessDatabaseCapacity", d, "Average")
        else:
            Q["dbcap"], Q["dbavg"] = ("AWS/RDS", "CPUUtilization", d, "Maximum"), ("AWS/RDS", "CPUUtilization", d, "Average")
    if p["mem"]:
        Q["mem"] = ("AWS/ECS", "MemoryUtilization", {"ClusterName": p["mem"]["cluster"], "ServiceName": p["mem"]["n"]}, "Maximum")
    for i, q in enumerate(p["queues"]):
        Q[f"q{i}"] = ("AWS/SQS", "NumberOfMessagesSent", {"QueueName": q}, "Sum")
    series, start = metric_hourly(cw, [(k, *v) for k, v in Q.items()])
    for i, tg in enumerate(tgs):
        by_service.append({"tg": tg["TargetGroupName"], "req": round(sum(series.pop(f"tg{i}", [])))})
    by_service.sort(key=lambda x: -x["req"])
    health = {r["tg"]: r["targets"] for lb_ in inv["lb"] if lb_["n"] == p["lb"] for r in lb_["routes"]}
    for x in by_service:
        x["targets"] = health.get(x["tg"], [])
    daily: list[dict] = []
    try:
        ce = s.client("ce", region_name="us-east-1")
        today = date.today()
        cost = ce.get_cost_and_usage(TimePeriod={"Start": str(today - timedelta(days=14)), "End": str(today)}, Granularity="DAILY",
                                     Metrics=["UnblendedCost"], Filter={"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Tax"]}}})
        daily = [{"d": r["TimePeriod"]["Start"], "usd": round(float(r["Total"]["UnblendedCost"]["Amount"]), 2)} for r in cost["ResultsByTime"]]
    except Exception as e:  # noqa: BLE001 - Cost Explorer may be disabled; the chart is then hidden
        print(f"warning: daily cost skipped ({type(e).__name__})")
    labels = {"lb": p["lb"], "db": db["n"] if db else None, "dbKind": ("acu" if db and db["serverless"] else "cpu"), "dbMax": (db or {}).get("maxAcu"),
              "mem": f'{p["mem"]["n"]}' if p["mem"] else None, "queues": p["queues"]}
    json.dump({"start": start, "hours": 72, "labels": labels, "series": series, "byService": by_service, "dailyCost": daily}, open(a.out, "w"), separators=(",", ":"))
    print(f"time series: {len(series)} series ({', '.join(f'{k}={v}' for k, v in labels.items() if v)}) -> {a.out}")


if __name__ == "__main__":
    main()
