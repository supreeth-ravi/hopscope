"""Build the serverless demo's data: Kestrel Coffee's order API on API Gateway and Lambda, in two regions (invented).

    python docs/demo-serverless/make_serverless.py

Every name, route, file, function, queue, metric and cost is invented. The script writes a small fake codebase and two
invented regional inventories (us-east-1 and eu-west-1: API Gateway, Lambda, DynamoDB, SQS with an event source mapping,
Step Functions with failed executions, EventBridge), merges them with Hopscope's own multi-region merge (aws_merge), and
runs the pipeline (code scan, call flows, linker, drift, rules, history) over the result.
"""
from __future__ import annotations

import json
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
COLLECTED = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
START = COLLECTED - timedelta(hours=72)
RNG = random.Random(5)

# ---------------------------------------------------------------- fake codebase

CODE = {
    "orders-api/pyproject.toml": '[project]\nname = "orders-api"\ndependencies = ["fastapi", "mangum", "boto3"]\n',
    "orders-api/app/__init__.py": "",
    "orders-api/app/aws.py": '''import json
import os

import boto3

dynamodb = boto3.client("dynamodb")
sqs = boto3.client("sqs")
sfn = boto3.client("stepfunctions")
events = boto3.client("events")
FULFIL_QUEUE = os.environ.get("FULFIL_QUEUE_URL", "kestrel-fulfil")


def save_order(order):
    dynamodb.put_item(TableName="kestrel-orders", Item={"id": {"S": order["id"]}})


def load_order(order_id):
    return dynamodb.get_item(TableName="kestrel-orders", Key={"id": {"S": order_id}})


def queue_fulfilment(order):
    sqs.send_message(QueueUrl=FULFIL_QUEUE, MessageBody=json.dumps(order))


def start_checkout(order):
    # payment, receipt and stock run as one workflow
    sfn.start_execution(stateMachineArn="arn:aws:states:::stateMachine:kestrel-checkout", name="kestrel-checkout", input=json.dumps(order))


def announce(order):
    events.put_events(Entries=[{"Source": "kestrel.orders", "DetailType": "order-placed", "Detail": json.dumps(order)}])
''',
    "orders-api/app/main.py": '''from fastapi import FastAPI
from mangum import Mangum

from app.aws import announce, load_order, queue_fulfilment, save_order, start_checkout

app = FastAPI()


@app.post("/orders")
def place_order(body: dict):
    """Take an order, charge it through the checkout workflow and hand it to fulfilment."""
    save_order(body)
    start_checkout(body)
    if body.get("subscription"):
        # subscription orders are fulfilled by the nightly roast instead
        announce(body)
    else:
        queue_fulfilment(body)
    return {"id": body["id"]}


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    return load_order(order_id)


@app.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str):
    order = load_order(order_id)
    announce({"cancelled": order_id})
    return order


handler = Mangum(app)
''',
    "fulfil-worker/pyproject.toml": '[project]\nname = "fulfil-worker"\ndependencies = ["boto3"]\n',
    "fulfil-worker/handler.py": '''import boto3

dynamodb = boto3.client("dynamodb")


def handler(event, context):
    # reads kestrel-fulfil through an event source mapping
    for record in event["Records"]:
        dynamodb.update_item(TableName="kestrel-orders", Key={"id": {"S": record["body"]}})
''',
}

# ---------------------------------------------------------------- fake AWS, one region at a time


def lam(n: str, inv: int, err: float, dur: float, mem: int = 512) -> dict:
    return {"n": n, "mem": mem, "inv": inv, "err": err, "dur": dur, "fam": n}


def region_inv(region: str) -> dict:
    """One region's inventory, as aws_inventory.regional() returns it (global services are added by the merge)."""
    eu = region == "eu-west-1"
    k = 0.25 if eu else 1.0
    sqs = [{"n": "kestrel-fulfil", "sent": round(41200 * k), "backlog": 3.0, "age": 6.0, "dlq": False, "fifo": False, "fam": "kestrel-fulfil"},
           {"n": "kestrel-fulfil-dlq", "sent": 0, "backlog": 0.0 if eu else 6.0, "age": 0.0 if eu else 50400.0, "dlq": True, "fifo": False, "fam": "kestrel-fulfil-dlq"}]
    lambdas = [lam("kestrel-orders-api", round(154000 * k), 12 * k, 88), lam("kestrel-fulfil-worker", round(41200 * k), 0, 140, 256),
               lam("kestrel-charge-card", round(9100 * k), 0 if eu else 31, 620, 256), lam("kestrel-send-receipt", round(9050 * k), 0, 210, 256)]
    if not eu:
        lambdas.append(lam("kestrel-roast-label", 2200, 0, 95, 256))
    sfn = [{"n": "kestrel-checkout", "type": "EXPRESS" if eu else "STANDARD", "started": round(9100 * k), "failed": 0 if eu else 23,
            "targets": [{"kind": "lambda", "n": "kestrel-charge-card"}, {"kind": "dynamodb", "n": "kestrel-orders"}, {"kind": "lambda", "n": "kestrel-send-receipt"}]}]
    esm = [{"fn": "kestrel-fulfil-worker", "kind": "sqs", "source": "kestrel-fulfil", "topics": [], "state": "Enabled", "last": "OK"}]
    rules = [] if eu else ["order-placed-to-roastery"]
    return {"ecs": [], "ec2": [], "rds": [], "lb": [], "sqs": sqs, "lambda": lambdas, "sns": [{"n": "kestrel-alerts", "published": 3 if not eu else 0}],
            "cache": [], "search": [], "apigw": [{"n": "kestrel-orders-api", "proto": "HTTP", "req": round(154000 * k), "fam": "kestrel-orders-api"}],
            "cognito": [{"n": "Kestrel Members"}] if not eu else [], "nat": 0, "eips": {"total": 0, "unattached": 0}, "ecr": 0, "secrets": 2,
            "dynamodb": ["kestrel-orders", "kestrel-members"] if not eu else ["kestrel-orders"], "ses": [] if eu else ["kestrel.example"], "eventbridge": rules,
            "sfn": sfn, "kinesis": [], "msk": [], "eks": [], "esm": esm,
            "ebTargets": [{"rule": r, "kind": "lambda", "n": "kestrel-roast-label"} for r in rules]}


def region_ts(region: str) -> dict:
    k = 0.25 if region == "eu-west-1" else 1.0
    q = [round(570 * k * (0.6 + 0.4 * abs(((h + 6) % 24) - 12) / 12) * RNG.uniform(0.9, 1.1)) for h in range(72)]
    return {"start": START.isoformat(), "hours": 72, "labels": {"lb": None, "db": None, "dbKind": "cpu", "dbMax": None, "mem": None, "queues": ["kestrel-fulfil"]},
            "series": {"q0": q}, "byService": []}


GLOBALS = {"kestrel-shop": {
    "cdn": [{"n": "kestrel-web", "origin": "kestrel-web-assets", "aliases": ["shop.kestrel.example"]}], "s3": {"count": 6},
    "cost": [["AWS Lambda", 142.6], ["Amazon DynamoDB", 96.4], ["Amazon API Gateway", 61.3], ["AWS Step Functions", 38.7], ["Amazon CloudFront", 22.1],
             ["Amazon Simple Queue Service", 9.8], ["Amazon EventBridge", 1.2]],
    "dailyCost": [{"d": str(COLLECTED.date() - timedelta(days=14 - i)), "usd": round(12.5 + RNG.uniform(-1, 1) + (2.5 if i >= 11 else 0), 2)} for i in range(14)]}}


def merged() -> tuple[dict, dict]:
    sys.path.insert(0, str(SCRIPTS))
    import aws_merge

    parts = [{"account": "kestrel-shop", "region": r, "inv": region_inv(r), "ts": region_ts(r)} for r in ("us-east-1", "eu-west-1")]
    skipped = [{"account": "kestrel-shop", "region": "ap-south-1", "why": "nothing found"}]
    return aws_merge.merge(parts, GLOBALS, skipped, COLLECTED.isoformat(timespec="seconds"))


# ---------------------------------------------------------------- pipeline


def run(script: str, *args: str) -> None:
    subprocess.run([sys.executable, str(SCRIPTS / script), *args], check=True)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, work = Path(tmp) / "kestrel-serverless", Path(tmp) / "work"
        data = work / "data"
        for rel, text in CODE.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text, encoding="utf-8")
        data.mkdir(parents=True)
        overrides = Path(tmp) / "hopscope.json"
        overrides.write_text(json.dumps({"company": "Kestrel Coffee serverless", "examples": ["Show me POST /orders", "Show me GET /orders/{order_id}"]}), encoding="utf-8")
        aws, ts = merged()
        (data / "aws.json").write_text(json.dumps(aws, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (data / "ts.json").write_text(json.dumps(ts, separators=(",", ":")), encoding="utf-8")
        run("code_scan.py", "--repo", str(repo), "--no-floorplan", "--out", str(data / "code.json"))
        run("call_flow.py", str(data / "code.json"), str(data / "flows.json"))
        run("linker.py", "--aws", str(data / "aws.json"), "--code", str(data / "code.json"), "--overrides", str(overrides), "--out", str(data / "links.json"))
        run("drift.py", "--data", str(data))
        run("rules.py", "--data", str(data), "--overrides", str(overrides))
        run("history.py", "--data", str(data), "--workdir", str(work))
        for f in SITE_FILES:
            if (data / f).exists():
                text = (data / f).read_text(encoding="utf-8").replace(str(repo), "/home/demo/kestrel-serverless").replace(tmp, "/home/demo")
                (OUT / f).write_text(text, encoding="utf-8")
    print(f"serverless demo data written to {OUT}")


if __name__ == "__main__":
    main()
