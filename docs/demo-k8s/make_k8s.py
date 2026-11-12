"""Build the Kubernetes demo's data: Kestrel Coffee on Kubernetes (EKS), a fictional company.

    python docs/demo-k8s/make_k8s.py

Every name, route, file, workload, event, metric and cost is invented. The script writes a small fake codebase and the
`kubectl get … -o json` output a cluster would give, runs it through Hopscope's own live-Kubernetes mapper (aws_eks with a
stand-in for kubectl), adds an invented AWS inventory, and runs the pipeline (code scan, call flows, linker, drift, rules,
history) twice, so the demo has a change between builds: the grinder's new image starts crash-looping.
"""
from __future__ import annotations

import json
import math
import random
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "hopscope" / "scripts"
OUT = Path(__file__).resolve().parent
SITE_FILES = ("code.json", "aws.json", "flows.json", "ts.json", "model.json", "changes.json", "drift.json")
COLLECTED = datetime(2026, 11, 9, 7, 0, tzinfo=timezone.utc)
START = COLLECTED - timedelta(hours=72)
RNG = random.Random(11)
CONTEXT = "kestrel-prod-eks"
REGISTRY = "registry.kestrel.example/kestrel"

# ---------------------------------------------------------------- fake codebase

DOCKERFILE = "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt\nCMD [\"python\", \"-m\", \"{mod}\"]\n"
CODE = {
    "shop-api/Dockerfile": DOCKERFILE.format(mod="app.main"),
    "shop-api/pyproject.toml": '[project]\nname = "shop-api"\ndependencies = ["fastapi", "psycopg", "redis", "kafka-python"]\n',
    "shop-api/app/__init__.py": "",
    "shop-api/app/db.py": '''import psycopg

conn = psycopg.connect(host="kestrel-db.cluster-xxxxxxxx.us-east-1.rds.amazonaws.com")


def fetch_all(sql, *args):
    return conn.execute(sql, args).fetchall()


def execute(sql, *args):
    return conn.execute(sql, args)
''',
    "shop-api/app/cache.py": '''import json

import redis

cache = redis.Redis(host="kestrel-cache.xxxxxxxx.use1.cache.amazonaws.com")


def get_json(key):
    raw = cache.get(key)
    return json.loads(raw) if raw else None


def set_json(key, value):
    cache.set(key, json.dumps(value), ex=300)
''',
    "shop-api/app/events.py": '''from kafka import KafkaProducer

producer = KafkaProducer(bootstrap_servers="kestrel-events.kafka.example:9092")


def publish_roast_order(order):
    # every paid order becomes a roast job for the roastery
    producer.send("roast-orders", order)
''',
    "shop-api/app/main.py": '''from fastapi import FastAPI

from app.cache import get_json, set_json
from app.db import execute, fetch_all
from app.events import publish_roast_order

app = FastAPI()


@app.get("/api/beans")
def list_beans():
    """The coffee beans on sale, cached for five minutes."""
    beans = get_json("beans")
    if beans is None:
        beans = fetch_all("SELECT id, name, origin FROM beans WHERE active")
        set_json("beans", beans)
    return beans


@app.post("/api/orders")
def place_order(body: dict):
    """Take an order and send it to the roastery."""
    order = execute("INSERT INTO orders (member_id, bean_id, grams) VALUES (%s, %s, %s) RETURNING id", body["member"], body["bean"], body["grams"])
    if body.get("whole_bean") is False:
        # ground orders also need the grinder
        execute("INSERT INTO grind_jobs (order_id) VALUES (%s)", order)
    publish_roast_order({"order": order})
    return {"id": order}


@app.get("/api/orders/{order_id}")
def get_order(order_id: int):
    return fetch_all("SELECT id, status FROM orders WHERE id = %s", order_id)
''',
    "storefront/Dockerfile": "FROM node:20-alpine\nCOPY . .\nRUN npm ci && npm run build\n",
    "storefront/package.json": '{"name": "storefront", "dependencies": {"react": "18.3.1"}}\n',
    "storefront/src/api.js": '''export const beans = () => fetch("/api/beans").then((r) => r.json());
export const order = (body) => fetch("/api/orders", { method: "POST", body: JSON.stringify(body) });
export const status = (id) => fetch(`/api/orders/${id}`).then((r) => r.json());
''',
    "roast-scheduler/Dockerfile": DOCKERFILE.format(mod="scheduler"),
    "roast-scheduler/pyproject.toml": '[project]\nname = "roast-scheduler"\ndependencies = ["kafka-python", "psycopg"]\n',
    "roast-scheduler/scheduler.py": '''import psycopg
from kafka import KafkaConsumer

consumer = KafkaConsumer("roast-orders", bootstrap_servers="kestrel-events.kafka.example:9092")
conn = psycopg.connect(host="kestrel-db.cluster-xxxxxxxx.us-east-1.rds.amazonaws.com")


def main():
    for message in consumer:
        conn.execute("INSERT INTO roast_batches (order_id) VALUES (%s)", (message.value,))
''',
    "grinder/Dockerfile": DOCKERFILE.format(mod="grinder"),
    "grinder/pyproject.toml": '[project]\nname = "grinder"\ndependencies = ["psycopg"]\n',
    "grinder/grinder.py": '''import os

import psycopg

conn = psycopg.connect(host=os.environ["GRIND_DB_HOST"])


def main():
    for job in conn.execute("SELECT order_id FROM grind_jobs WHERE done = false"):
        conn.execute("UPDATE grind_jobs SET done = true WHERE order_id = %s", (job[0],))
''',
}

# ---------------------------------------------------------------- fake cluster (kubectl get … -o json)


def meta(ns: str, name: str, labels: dict | None = None) -> dict:
    return {"name": name, "namespace": ns, "labels": labels or {"app": name}}


def deployment(ns: str, name: str, image: str, replicas: int, ready: int, env: dict | None = None) -> dict:
    container = {"name": name, "image": f"{REGISTRY}/{image}", "env": [{"name": k, "value": v} for k, v in (env or {}).items()]}
    return {"kind": "Deployment", "apiVersion": "apps/v1", "metadata": meta(ns, name), "status": {"readyReplicas": ready, "replicas": replicas},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}}, "template": {"metadata": {"labels": {"app": name}}, "spec": {"containers": [container]}}}}


def service(ns: str, name: str) -> dict:
    return {"kind": "Service", "apiVersion": "v1", "metadata": meta(ns, name), "spec": {"type": "ClusterIP", "selector": {"app": name}, "ports": [{"port": 80, "targetPort": 8080}]}}


def cluster_objects(hour: float) -> list[dict]:
    """The cluster `hour` hours into the window: the grinder's 1.5.0 image went out at hour 50 and has crashed since."""
    grinder_new = hour >= 50
    ingress = {"kind": "Ingress", "apiVersion": "networking.k8s.io/v1", "metadata": meta("shop", "shop-ingress"),
               "spec": {"ingressClassName": "alb", "rules": [{"host": "shop.kestrel.example", "http": {"paths": [
                   {"path": "/api", "pathType": "Prefix", "backend": {"service": {"name": "shop-api", "port": {"number": 80}}}},
                   {"path": "/", "pathType": "Prefix", "backend": {"service": {"name": "storefront", "port": {"number": 80}}}}]}}]}}
    hpa = {"kind": "HorizontalPodAutoscaler", "apiVersion": "autoscaling/v2", "metadata": meta("shop", "shop-api"),
           "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "shop-api"}, "minReplicas": 3, "maxReplicas": 12}}
    cron = {"kind": "CronJob", "apiVersion": "batch/v1", "metadata": meta("roastery", "nightly-roast-plan"),
            "spec": {"schedule": "0 2 * * *", "jobTemplate": {"spec": {"template": {"spec": {"containers": [{"name": "plan", "image": f"{REGISTRY}/roast-scheduler:2.3.0"}]}}}}}}
    return [
        deployment("shop", "shop-api", "shop-api:1.8.2", 3, 3, {"KAFKA_TOPIC": "roast-orders"}), deployment("shop", "storefront", "storefront:4.1.0", 2, 2),
        deployment("roastery", "roast-scheduler", "roast-scheduler:2.3.0", 1, 1),
        deployment("roastery", "grinder", f"grinder:{'1.5.0' if grinder_new else '1.4.0'}", 2, 0 if grinder_new else 2),
        service("shop", "shop-api"), service("shop", "storefront"), ingress, hpa, cron,
    ]


def cluster_events(hour: float) -> list[dict]:
    if hour < 50:
        return []
    when = (START + timedelta(hours=hour - 0.3)).isoformat().replace("+00:00", "Z")
    pod = {"kind": "Pod", "namespace": "roastery", "name": "grinder-7d9f8c6b5-x2k4q"}
    return [{"type": "Warning", "reason": "BackOff", "message": "Back-off restarting failed container grinder in pod grinder-7d9f8c6b5-x2k4q",
             "count": 41, "lastTimestamp": when, "involvedObject": pod, "metadata": {"name": "grinder.1"}}]


def kube_rows(hour: float) -> dict:
    """The cluster through Hopscope's own live mapper, with kubectl replaced by the invented objects above."""
    sys.path.insert(0, str(SCRIPTS))
    import aws_eks

    def fake_kubectl(ctx: str, *args: str) -> dict:
        return {"items": cluster_events(hour) if "events" in args else cluster_objects(hour)}

    aws_eks.kubectl = fake_kubectl
    return aws_eks.collect_kube([CONTEXT], now=START + timedelta(hours=hour))


def inventory(hour: float) -> dict:
    kube = kube_rows(hour)
    rds_host = "xxxxxxxx.us-east-1.rds.amazonaws.com"
    alb = {"n": "k8s-shop-shopingress", "type": "application", "scheme": "internet-facing", "req": 268400, "err": 410, "e503": 120, "t5xx": 290,
           "routes": [{"port": 443, "when": "host-header=shop.kestrel.example & path-pattern=/api", "tg": "k8s-shop-shopapi", "services": [], "targets": ["healthy"] * 3, "instances": []},
                      {"port": 443, "when": "host-header=shop.kestrel.example", "tg": "k8s-shop-storefront", "services": [], "targets": ["healthy"] * 2, "instances": []}]}
    return {
        "collectedAt": (START + timedelta(hours=hour)).isoformat(), "region": "us-east-1", "account": "••••••••0000",
        "ecs": kube.get("ecs", []), "ec2": [], "lb": [alb, *kube.get("lb", [])],
        "rds": [{"n": "kestrel-db", "instance": "kestrel-db-instance-1", "engine": "aurora-postgresql", "class": "db.r6g.large", "serverless": False, "minAcu": None,
                 "maxAcu": None, "acu": 0, "acuPeak": 0, "cpu": 34.0, "conns": 52.0, "endpoint": f"kestrel-db-instance-1.{rds_host}", "clusterEndpoint": f"kestrel-db.cluster-{rds_host}", "env": "prod"}],
        "sqs": [], "lambda": [{"n": "roast-label-printer", "mem": 256, "inv": 2210, "err": 0.0, "dur": 140.0, "fam": "roast-label-printer"}],
        "cost": [["Amazon Elastic Kubernetes Service", 219.0], ["Amazon Elastic Compute Cloud - Compute", 486.2], ["Amazon Relational Database Service", 301.5],
                 ["Amazon Managed Streaming for Apache Kafka", 288.0], ["Amazon ElastiCache", 48.9], ["Amazon Elastic Load Balancing", 31.4], ["AWS Lambda", 4.2]],
        "sns": [], "cache": [{"n": "kestrel-cache-001", "type": "cache.t4g.small", "engine": "valkey", "cpu": 4.1, "mem": 38.0, "conns": 41.0}],
        "search": [], "apigw": [], "cdn": [], "cognito": [], "s3": {"count": 4}, "nat": 2, "eips": {"total": 2, "unattached": 0},
        "ecr": 4, "secrets": 6, "dynamodb": [], "ses": [], "eventbridge": [], "sfn": [], "kinesis": [],
        "msk": [{"n": "kestrel-events", "type": "PROVISIONED", "brokers": 3, "instance": "kafka.m7g.large", "version": "3.7.x"}],
        "eks": [{"n": "kestrel-prod", "version": "1.31", "status": "ACTIVE",
                 "nodegroups": [{"n": "general", "status": "ACTIVE", "types": ["m7g.large"], "capacity": "ON_DEMAND", "min": 3, "max": 9, "desired": 4}]}],
        "ebTargets": [],
        "esm": [{"fn": "roast-label-printer", "kind": "msk", "source": "kestrel-events", "topics": ["roast-orders"], "state": "Enabled", "last": "OK"}],
    }


# ---------------------------------------------------------------- fake hourly metrics


def shape(h: int) -> float:
    local = (h + 5) % 24
    return 0.5 + 0.5 * math.exp(-((local - 9) ** 2) / 8) + 0.3 * math.exp(-((local - 19) ** 2) / 10)


def timeseries() -> dict:
    hours = 72
    req = [round(3700 * shape(h) * RNG.uniform(0.93, 1.07)) for h in range(hours)]
    e5 = [round(r * 0.001 * RNG.uniform(0.5, 1.5)) for r in req]
    e503 = [round(r * 0.0004 * RNG.uniform(0.5, 1.5)) for r in req]
    series = {"req": req, "elb5xx": [a + b for a, b in zip(e5, e503, strict=True)], "elb503": e503, "e5xx": e5,
              "p95": [round(0.21 * shape(h) * RNG.uniform(0.9, 1.1), 3) for h in range(hours)], "p50": [round(0.06 * shape(h) * RNG.uniform(0.9, 1.1), 3) for h in range(hours)],
              "dbcap": [round(min(96, 22 + 40 * shape(h) * RNG.uniform(0.9, 1.1)), 1) for h in range(hours)], "dbavg": [round(18 + 22 * shape(h), 1) for h in range(hours)]}
    total = sum(req)
    by_service = [{"tg": "k8s-shop-shopapi", "req": round(total * 0.41), "targets": ["healthy"] * 3}, {"tg": "k8s-shop-storefront", "req": round(total * 0.59), "targets": ["healthy"] * 2}]
    first = COLLECTED.date() - timedelta(days=14)
    daily = [{"d": str(first + timedelta(days=i)), "usd": round(46 + RNG.uniform(-2, 2), 2)} for i in range(14)]
    labels = {"lb": "k8s-shop-shopingress", "db": "kestrel-db", "dbKind": "cpu", "dbMax": None, "mem": None, "queues": []}
    return {"start": START.isoformat(), "hours": hours, "labels": labels, "series": series, "byService": by_service, "dailyCost": daily}


# ---------------------------------------------------------------- pipeline


def run(script: str, *args: str) -> None:
    subprocess.run([sys.executable, str(SCRIPTS / script), *args], check=True)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, work = Path(tmp) / "kestrel-k8s", Path(tmp) / "work"
        data = work / "data"
        for rel, text in CODE.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text, encoding="utf-8")
        data.mkdir(parents=True)
        overrides = Path(tmp) / "hopscope.json"
        overrides.write_text(json.dumps({"company": "Kestrel Coffee on Kubernetes", "examples": ["Show me POST /api/orders", "Show me GET /api/beans"]}), encoding="utf-8")
        run("code_scan.py", "--repo", str(repo), "--no-floorplan", "--out", str(data / "code.json"))
        run("call_flow.py", str(data / "code.json"), str(data / "flows.json"))
        (data / "ts.json").write_text(json.dumps(timeseries(), separators=(",", ":")), encoding="utf-8")
        for hour in (40, 72):  # an earlier build before the grinder's new image, then this one
            (data / "aws.json").write_text(json.dumps(inventory(hour), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            run("linker.py", "--aws", str(data / "aws.json"), "--code", str(data / "code.json"), "--overrides", str(overrides), "--out", str(data / "links.json"))
            run("drift.py", "--data", str(data))
            run("rules.py", "--data", str(data), "--overrides", str(overrides))
            run("history.py", "--data", str(data), "--workdir", str(work))
        for f in SITE_FILES:
            if (data / f).exists():
                text = (data / f).read_text(encoding="utf-8").replace(str(repo), "/home/demo/kestrel-k8s").replace(tmp, "/home/demo")
                (OUT / f).write_text(text, encoding="utf-8")
    print(f"Kubernetes demo data written to {OUT}")


if __name__ == "__main__":
    main()
