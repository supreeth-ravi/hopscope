"""Build the live demo's data from a fictional company, Kestrel Coffee (an online coffee subscription shop).

    python docs/demo/make_demo.py

Every name, route, file, queue, metric and cost here is invented. The script writes a small fake codebase
and a fake AWS inventory, then runs Hopscope's own pipeline (code scan, call flows, linker, rules) over them,
so the demo behaves like a real run without touching any real account or code.
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
SITE_FILES = ("code.json", "aws.json", "flows.json", "ts.json", "model.json")
COLLECTED = datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)
RNG = random.Random(7)
SQS = "https://sqs.us-east-1.amazonaws.com/************/"

# ---------------------------------------------------------------- fake codebase

DOCKERFILE = "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nRUN pip install -r requirements.txt\nCMD [\"python\", \"-m\", \"{mod}\"]\n"

CODE: dict[str, str] = {
    # shop-api: the public API behind shop.kestrel.example
    "shop-api/Dockerfile": DOCKERFILE.format(mod="uvicorn app.main:app"),
    "shop-api/requirements.txt": "fastapi\nsqlalchemy\nredis\nboto3\nhttpx\n",
    "shop-api/app/__init__.py": "",
    "shop-api/app/main.py": '''from fastapi import FastAPI

from app.routes import account, cart, catalog, orders, subscriptions, webhooks

app = FastAPI(title="Kestrel shop API")
for module in (account, cart, catalog, orders, subscriptions, webhooks):
    app.include_router(module.router)


@app.get("/health")
def health():
    return {"ok": True}
''',
    "shop-api/app/db.py": '''from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

engine = create_engine("postgresql+psycopg://kestrel-db/shop")
Session = sessionmaker(bind=engine)


def fetch_one(sql, **params):
    with Session() as s:
        return s.execute(text(sql), params).mappings().first()


def execute(sql, **params):
    with Session() as s:
        s.execute(text(sql), params)
        s.commit()
''',
    "shop-api/app/cache.py": '''import json

import redis

redis_cache = redis.Redis(host="kestrel-cache", port=6379)


def get_json(key):
    raw = redis_cache.get(key)
    return json.loads(raw) if raw else None


def set_json(key, value, ttl=300):
    redis_cache.setex(key, ttl, json.dumps(value))
''',
    "shop-api/app/queue.py": '''import json

import boto3

sqs = boto3.client("sqs")
ORDERS = "kestrel-orders.fifo"


def publish_order(order):
    sqs.send_message(QueueUrl=ORDERS, MessageBody=json.dumps(order), MessageGroupId=str(order["customer_id"]))
''',
    "shop-api/app/identity.py": '''import boto3

idp = boto3.client("cognito-idp")
POOL_CLIENT = "kestrel-members-web"


def check_password(email, password):
    return idp.initiate_auth(ClientId=POOL_CLIENT, AuthFlow="USER_PASSWORD_AUTH",
                             AuthParameters={"USERNAME": email, "PASSWORD": password})


def create_member(email, password):
    return idp.sign_up(ClientId=POOL_CLIENT, Username=email, Password=password)
''',
    "shop-api/app/payments.py": '''import httpx

PROCESSOR = "https://payments.processor.example/v1/charges"


def charge_card(customer_id, amount_cents, token):
    r = httpx.post(PROCESSOR, json={"customer": customer_id, "amount": amount_cents, "source": token}, timeout=10)
    r.raise_for_status()
    return r.json()
''',
    "shop-api/app/routes/__init__.py": "",
    "shop-api/app/routes/account.py": '''from fastapi import APIRouter

from app import db
from app.identity import check_password, create_member

router = APIRouter(prefix="/account")


def load_member(email):
    return db.fetch_one("select * from members where email = :e", e=email)


def record_sign_in(member_id):
    db.execute("update members set last_seen = now() where id = :i", i=member_id)


@router.post("/sign-in")
def sign_in(body: dict):
    tokens = check_password(body["email"], body["password"])
    member = load_member(body["email"])
    record_sign_in(member["id"])
    return {"member": member["id"], "tokens": tokens}


@router.post("/sign-up")
def sign_up(body: dict):
    create_member(body["email"], body["password"])
    db.execute("insert into members (email) values (:e)", e=body["email"])
    return {"ok": True}
''',
    "shop-api/app/routes/catalog.py": '''from fastapi import APIRouter

from app import db
from app.cache import get_json, set_json

router = APIRouter(prefix="/catalog")


@router.get("/beans")
def list_beans():
    cached = get_json("beans:all")
    if cached:
        return cached
    beans = db.fetch_one("select json_agg(b) as items from beans b where in_stock")
    set_json("beans:all", beans, ttl=600)
    return beans
''',
    "shop-api/app/routes/cart.py": '''from fastapi import APIRouter

from app import db
from app.cache import get_json
from app.payments import charge_card
from app.queue import publish_order

router = APIRouter(prefix="/cart")


def price_cart(cart):
    return sum(line["qty"] * line["price_cents"] for line in cart["lines"])


def reserve_stock(cart):
    for line in cart["lines"]:
        db.execute("update beans set stock = stock - :q where id = :i", q=line["qty"], i=line["bean_id"])


@router.post("/checkout")
def checkout(body: dict):
    cart = get_json(f"cart:{body['customer_id']}")
    total = price_cart(cart)
    reserve_stock(cart)
    charge = charge_card(body["customer_id"], total, body["card_token"])
    order = {"customer_id": body["customer_id"], "total": total, "charge": charge["id"], "lines": cart["lines"]}
    db.execute("insert into orders (customer_id, total, charge_id) values (:c, :t, :x)", c=body["customer_id"], t=total, x=charge["id"])
    publish_order(order)
    return {"ok": True, "total": total}
''',
    "shop-api/app/routes/orders.py": '''from fastapi import APIRouter

from app import db
from app.cache import get_json

router = APIRouter(prefix="/orders")


@router.get("/{order_id}")
def get_order(order_id: int):
    cached = get_json(f"order:{order_id}")
    return cached or db.fetch_one("select * from orders where id = :i", i=order_id)
''',
    "shop-api/app/routes/subscriptions.py": '''from fastapi import APIRouter

from app import db

router = APIRouter(prefix="/subscriptions")


@router.post("")
def start_subscription(body: dict):
    db.execute("insert into subscriptions (customer_id, bean_id, every_days) values (:c, :b, :d)",
               c=body["customer_id"], b=body["bean_id"], d=body["every_days"])
    return {"ok": True}


@router.post("/{sub_id}/skip")
def skip_delivery(sub_id: int):
    db.execute("update subscriptions set skip_next = true where id = :i", i=sub_id)
    return {"ok": True}
''',
    "shop-api/app/routes/webhooks.py": '''from fastapi import APIRouter

from app import db
from app.queue import publish_order

router = APIRouter(prefix="/webhooks")


@router.post("/payments")
def payment_event(body: dict):
    db.execute("update orders set paid = :p where charge_id = :x", p=body["type"] == "charge.succeeded", x=body["charge"])
    if body["type"] == "charge.refunded":
        publish_order({"customer_id": body["customer"], "refund": body["charge"]})
    return {"ok": True}
''',
    # order-worker: turns paid orders into roast batches and receipts
    "order-worker/Dockerfile": DOCKERFILE.format(mod="worker"),
    "order-worker/requirements.txt": "boto3\nsqlalchemy\n",
    "order-worker/worker.py": '''import json

import boto3
from sqlalchemy import create_engine, text

sqs = boto3.client("sqs")
s3 = boto3.client("s3")
engine = create_engine("postgresql+psycopg://kestrel-db/shop")
ORDERS, NOTIFY = "kestrel-orders.fifo", "kestrel-notify.fifo"


def add_to_roast_batch(order):
    with engine.begin() as c:
        c.execute(text("insert into roast_queue (order_id) values (:o)"), {"o": order["charge"]})


def store_receipt(order):
    s3.put_object(Bucket="kestrel-receipts", Key=f"{order['charge']}.json", Body=json.dumps(order))


def main():
    while True:
        for m in sqs.receive_message(QueueUrl=ORDERS, WaitTimeSeconds=20).get("Messages", []):
            order = json.loads(m["Body"])
            add_to_roast_batch(order)
            store_receipt(order)
            sqs.send_message(QueueUrl=NOTIFY, MessageBody=json.dumps({"kind": "order-confirmed", **order}), MessageGroupId="mail")
            sqs.delete_message(QueueUrl=ORDERS, ReceiptHandle=m["ReceiptHandle"])


if __name__ == "__main__":
    main()
''',
    # notify-worker: sends order and shipping emails
    "notify-worker/Dockerfile": DOCKERFILE.format(mod="worker"),
    "notify-worker/requirements.txt": "boto3\n",
    "notify-worker/worker.py": '''import json

import boto3

sqs = boto3.client("sqs")
ses = boto3.client("sesv2")
NOTIFY = "kestrel-notify.fifo"


def render(event):
    return f"Thanks! Your order of {len(event.get('lines', []))} bags is on the roaster."


def main():
    while True:
        for m in sqs.receive_message(QueueUrl=NOTIFY, WaitTimeSeconds=20).get("Messages", []):
            event = json.loads(m["Body"])
            ses.send_email(FromEmailAddress="hello@kestrel.example", Destination={"ToAddresses": [event["email"]]},
                           Content={"Simple": {"Subject": {"Data": "Order confirmed"}, "Body": {"Text": {"Data": render(event)}}}})
            sqs.delete_message(QueueUrl=NOTIFY, ReceiptHandle=m["ReceiptHandle"])


if __name__ == "__main__":
    main()
''',
    # roast-planner: nightly job that plans tomorrow's roasts
    "roast-planner/Dockerfile": DOCKERFILE.format(mod="plan"),
    "roast-planner/requirements.txt": "boto3\nsqlalchemy\n",
    "roast-planner/plan.py": '''import json

import boto3
from sqlalchemy import create_engine, text

engine = create_engine("postgresql+psycopg://kestrel-db/shop")
s3 = boto3.client("s3")


def main():
    with engine.begin() as c:
        rows = c.execute(text("select bean_id, sum(qty) from roast_queue group by bean_id")).all()
    s3.put_object(Bucket="kestrel-roast-plans", Key="tomorrow.json", Body=json.dumps([list(r) for r in rows]))


if __name__ == "__main__":
    main()
''',
    # storefront: the web shop
    "storefront/package.json": '{"name": "kestrel-storefront", "dependencies": {"express": "^4"}}',
    "storefront/Dockerfile": "FROM node:20-slim\nWORKDIR /app\nCOPY . .\nRUN npm ci\nCMD [\"node\", \"server.js\"]\n",
    "storefront/server.js": '''const express = require('express');
const { renderPage } = require('./render');

const app = express();
app.get('/', (req, res) => res.send(renderPage('home')));
app.get('/beans/:slug', (req, res) => res.send(renderPage('bean', req.params.slug)));
app.get('/subscribe', (req, res) => res.send(renderPage('subscribe')));
app.listen(3000);
''',
    "storefront/render.js": '''function renderPage(name, slug) {
  return `<!doctype html><title>Kestrel Coffee</title><main data-page="${name}" data-slug="${slug || ''}"></main>`;
}
module.exports = { renderPage };
''',
    # admin: the staff console
    "admin/package.json": '{"name": "kestrel-admin", "dependencies": {"express": "^4", "pg": "^8"}}',
    "admin/Dockerfile": "FROM node:20-slim\nWORKDIR /app\nCOPY . .\nRUN npm ci\nCMD [\"node\", \"server.js\"]\n",
    "admin/server.js": '''const express = require('express');
const { Pool } = require('pg');

const db = new Pool({ host: 'kestrel-db', database: 'shop' });
const app = express();
app.get('/orders', async (req, res) => res.json((await db.query('select * from orders order by id desc limit 50')).rows));
app.post('/beans/:id/stock', async (req, res) => res.json((await db.query('update beans set stock = $1 where id = $2', [req.body.stock, req.params.id])).rowCount));
app.listen(4000);
''',
}


def write_codebase(base: Path) -> None:
    for rel, text in CODE.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- fake AWS inventory

def ecs(cluster, n, image, env, cpu, mem, size="0.5 vCPU · 1 GB", refs=(), tgs=(), running=1, own=True, container=None):
    return {"cluster": cluster, "n": n, "running": running, "desired": running, "size": size, "images": [image], "ownImage": own,
            "containers": [container or n], "refs": [{"k": k, "v": v} for k, v in refs], "tgs": ["arn:aws:redacted"] if tgs else [],
            "env": env, "cpu": cpu, "mem": mem, "memMax": round(mem * 1.08, 3), "cpuMax": round(cpu * 3.1, 3)}


def inventory() -> dict:
    prod, stg = "prod-kestrel", "staging-kestrel"
    queue_refs = [("ORDERS_QUEUE_URL", SQS + "kestrel-orders.fifo"), ("NOTIFY_QUEUE_URL", SQS + "kestrel-notify.fifo")]
    redis = ("REDIS_URL", "rediss://master.kestrel-cache.xxxxxxxx.use1.cache.amazonaws.com:6379")
    services = [
        ecs(prod, "shop-api", "kestrel-shop-api", "prod", 18.4, 61.2, "1 vCPU · 2 GB", [redis, queue_refs[0]], tgs=True, running=2),
        ecs(prod, "storefront", "kestrel-storefront", "prod", 9.7, 38.5, tgs=True, running=2),
        ecs(prod, "order-worker", "kestrel-order-worker", "prod", 4.2, 33.0, "0.25 vCPU · 0.5 GB", queue_refs),
        ecs(prod, "notify-worker", "kestrel-notify-worker", "prod", 1.1, 22.4, "0.25 vCPU · 0.5 GB", [queue_refs[1]]),
        ecs(prod, "admin", "kestrel-admin", "prod", 0.6, 18.9, "0.25 vCPU · 0.5 GB", tgs=True),
        ecs(prod, "roast-planner", "kestrel-roast-planner", "prod", 0.0, 0.0, "0.5 vCPU · 1 GB", running=0),
        ecs(prod, "recommendations", "kestrel-reco", "prod", 0.4, 11.2, "2 vCPU · 8 GB"),  # forgotten experiment: big and idle
        ecs(prod, "grafana", "grafana/grafana", "prod", 0.8, 24.0, "0.5 vCPU · 1 GB", own=False),
        ecs(stg, "shop-api", "kestrel-shop-api", "staging", 0.9, 30.2, tgs=True),
        ecs(stg, "storefront", "kestrel-storefront", "staging", 0.5, 25.1, tgs=True),
        ecs(stg, "order-worker", "kestrel-order-worker", "staging", 0.2, 20.0, "0.25 vCPU · 0.5 GB", queue_refs),
    ]
    alb_routes = [
        {"port": 443, "when": "path-pattern=/api/* & host-header=shop.kestrel.example", "tg": "shop-api", "services": [f"{prod}/shop-api"], "targets": ["healthy"], "instances": []},
        {"port": 443, "when": "host-header=admin.kestrel.example", "tg": "admin", "services": [f"{prod}/admin"], "targets": ["healthy"], "instances": []},
        {"port": 443, "when": "host-header=staging.kestrel.example", "tg": "staging-shop", "services": [f"{stg}/shop-api", f"{stg}/storefront"], "targets": ["healthy"], "instances": []},
        {"port": 443, "when": "default", "tg": "storefront", "services": [f"{prod}/storefront"], "targets": ["healthy"], "instances": []},
        {"port": 443, "when": "path-pattern=/legacy/*", "tg": "legacy-shop", "services": [], "targets": [], "instances": []},
    ]
    sqs = [
        {"n": "kestrel-orders.fifo", "sent": 18420, "backlog": 2.0, "age": 4.0, "dlq": False, "fifo": True, "fam": "kestrel-orders.fifo"},
        {"n": "kestrel-notify.fifo", "sent": 21110, "backlog": 0.0, "age": 1.0, "dlq": False, "fifo": True, "fam": "kestrel-notify.fifo"},
        {"n": "kestrel-orders-dlq.fifo", "sent": 0, "backlog": 37.0, "age": 151200.0, "dlq": True, "fifo": True, "fam": "kestrel-orders-dlq.fifo"},
        {"n": "staging-kestrel-orders.fifo", "sent": 12, "backlog": 0.0, "age": 0.0, "dlq": False, "fifo": True, "fam": "staging-kestrel-orders.fifo"},
    ] + [{"n": f"promo-campaign-{i:02d}", "sent": 0, "backlog": 0.0, "age": 0.0, "dlq": False, "fifo": False, "fam": "promo-campaign-*"} for i in range(1, 15)]
    lambdas = [
        {"n": "image-resize", "mem": 1024, "inv": 3820, "err": 0.0, "dur": 412.0, "fam": "image-resize"},
        {"n": "stock-sync", "mem": 256, "inv": 288, "err": 3.0, "dur": 950.0, "fam": "stock-sync"},
        {"n": "wholesale-quote", "mem": 512, "inv": 41, "err": 0.0, "dur": 230.0, "fam": "wholesale-quote"},
    ] + [{"n": f"promo-landing-{i:03d}", "mem": 128, "inv": 0, "err": 0.0, "dur": 0.0, "fam": "promo-landing-*"} for i in range(1, 61)]
    rds_host = "xxxxxxxx.us-east-1.rds.amazonaws.com"
    rds = [
        {"n": "kestrel-db", "instance": "kestrel-db-instance-1", "engine": "aurora-postgresql", "class": "db.serverless", "serverless": True, "minAcu": 0.5,
         "maxAcu": 4.0, "acu": 1.6, "acuPeak": 3.9, "cpu": 21.4, "conns": 64.0, "endpoint": f"kestrel-db-instance-1.{rds_host}",
         "clusterEndpoint": f"kestrel-db.cluster-{rds_host}", "env": "prod"},
        {"n": "kestrel-db-staging", "instance": "kestrel-db-staging-instance-1", "engine": "aurora-postgresql", "class": "db.serverless", "serverless": True,
         "minAcu": 0.5, "maxAcu": 2.0, "acu": 0.5, "acuPeak": 0.6, "cpu": 2.1, "conns": 6.0, "endpoint": f"kestrel-db-staging-instance-1.{rds_host}",
         "clusterEndpoint": f"kestrel-db-staging.cluster-{rds_host}", "env": "staging"},
        {"n": "analytics-old", "instance": "analytics-old-instance-1", "engine": "aurora-postgresql", "class": "db.serverless", "serverless": True, "minAcu": 0.0,
         "maxAcu": 2.0, "acu": 0.04, "acuPeak": 0.5, "cpu": 1.2, "conns": 0.0, "endpoint": f"analytics-old-instance-1.{rds_host}",
         "clusterEndpoint": f"analytics-old.cluster-{rds_host}", "env": "prod"},
    ]
    return {
        "collectedAt": COLLECTED.isoformat(), "region": "us-east-1", "account": "••••••••0000",
        "ecs": services,
        "ec2": [
            {"id": "i-00000000000000001", "n": "bastion", "type": "t4g.nano", "state": "running", "env": "prod", "cpu": 0.4, "cpuMax": 2.1,
             "credits": "standard", "creditMin": 140.0, "surplus": 0.0, "diskGB": 8},
            {"id": "i-00000000000000002", "n": "build-runner", "type": "c6i.4xlarge", "state": "running", "env": "staging", "cpu": 1.7, "cpuMax": 9.5,
             "credits": None, "creditMin": None, "surplus": 0.0, "diskGB": 200},
        ],
        "rds": rds,
        "lb": [{"n": "kestrel-public", "type": "application", "scheme": "internet-facing", "req": 412880, "err": 2310, "e503": 1460, "t5xx": 850, "routes": alb_routes}],
        "sqs": sqs, "lambda": lambdas,
        "cost": [["Amazon Elastic Container Service", 612.40], ["Amazon Relational Database Service", 498.15], ["Amazon Elastic Compute Cloud - Compute", 441.80],
                 ["Amazon Elastic Load Balancing", 96.30], ["EC2 - Other", 71.25], ["Amazon ElastiCache", 48.90], ["Amazon Simple Storage Service", 37.10],
                 ["AWS Lambda", 22.60], ["Amazon CloudFront", 19.85], ["Amazon Simple Email Service", 6.40], ["Amazon Simple Queue Service", 3.10]],
        "sns": [{"n": "ops-alerts", "published": 4}],
        "cache": [{"n": "kestrel-cache-001", "type": "cache.t4g.small", "engine": "cache", "cpu": 3.2, "mem": 41.5, "conns": 38.0}],
        "search": [],
        "apigw": [{"n": "wholesale-partners", "proto": "REST", "req": 1820, "fam": "wholesale-partners"}],
        "cdn": [{"n": "cdn-1", "origin": "origin", "aliases": ["cdn.kestrel.example"]}],
        "cognito": [{"n": "Kestrel Members"}],
        "s3": {"count": 9}, "nat": 1, "eips": {"total": 3, "unattached": 1},
        "ecr": 7, "secrets": 11, "dynamodb": ["tf-lock"], "ses": ["kestrel.example"], "eventbridge": ["roast-planner-nightly"],
    }


# ---------------------------------------------------------------- fake hourly metrics

def daily_shape(h: int) -> float:
    local = (h + 5) % 24  # shoppers' evening peak
    return 0.55 + 0.45 * math.exp(-((local - 20) ** 2) / 18) + 0.25 * math.exp(-((local - 8) ** 2) / 6)


def timeseries() -> dict:
    hours, start = 72, COLLECTED - timedelta(hours=72)
    req = [round(5200 * daily_shape(h) * RNG.uniform(0.93, 1.07)) for h in range(hours)]
    spike = range(50, 54)  # a short checkout outage two days in
    e503 = [round(r * (0.06 if h in spike else 0.0015) * RNG.uniform(0.6, 1.4)) for h, r in enumerate(req)]
    e5xx = [round(r * (0.02 if h in spike else 0.001) * RNG.uniform(0.5, 1.5)) for h, r in enumerate(req)]
    series = {
        "req": req, "elb5xx": [a + b for a, b in zip(e503, e5xx, strict=True)], "elb503": e503, "e5xx": e5xx,
        "p95": [round(0.32 * daily_shape(h) * (3.5 if h in spike else 1) * RNG.uniform(0.9, 1.1), 3) for h in range(hours)],
        "p50": [round(0.08 * daily_shape(h) * RNG.uniform(0.9, 1.1), 3) for h in range(hours)],
        "dbcap": [round(min(4.0, 1.2 + 2.6 * daily_shape(h) - 1.4 + (1.2 if h in spike else 0)), 2) for h in range(hours)],
        "dbavg": [round(min(4.0, 0.9 + 1.6 * daily_shape(h) - 0.9), 2) for h in range(hours)],
        "mem": [round(52 + 14 * daily_shape(h) * RNG.uniform(0.95, 1.05), 1) for h in range(hours)],
        "q0": [round(r * 0.045 * RNG.uniform(0.8, 1.2)) for r in req],
        "q1": [round(r * 0.052 * RNG.uniform(0.8, 1.2)) for r in req],
    }
    tg_req = {"storefront": 0.55, "shop-api": 0.38, "admin": 0.01, "staging-shop": 0.06}
    total = sum(req)
    by_service = [{"tg": tg, "req": round(total * f), "targets": ["healthy"]} for tg, f in tg_req.items()]
    by_service.append({"tg": "legacy-shop", "req": 0, "targets": []})
    first = COLLECTED.date() - timedelta(days=14)
    daily = [{"d": str(first + timedelta(days=i)), "usd": round(58 + RNG.uniform(-3, 3) + (14 if i == 12 else 0), 2)} for i in range(14)]
    labels = {"lb": "kestrel-public", "db": "kestrel-db", "dbKind": "acu", "dbMax": 4.0, "mem": "shop-api",
              "queues": ["kestrel-orders.fifo", "kestrel-notify.fifo"]}
    return {"start": start.isoformat(), "hours": hours, "labels": labels, "series": series, "byService": by_service, "dailyCost": daily}


# ---------------------------------------------------------------- pipeline

def run(script: str, *args: str) -> None:
    subprocess.run([sys.executable, str(SCRIPTS / script), *args], check=True)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, data = Path(tmp) / "kestrel", Path(tmp) / "data"
        write_codebase(repo)
        data.mkdir()
        overrides = Path(tmp) / "hopscope.json"
        overrides.write_text(json.dumps({"company": "Kestrel Coffee"}), encoding="utf-8")
        (data / "aws.json").write_text(json.dumps(inventory(), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (data / "ts.json").write_text(json.dumps(timeseries(), separators=(",", ":")), encoding="utf-8")
        run("code_scan.py", "--repo", str(repo), "--no-floorplan", "--out", str(data / "code.json"))
        run("call_flow.py", str(data / "code.json"), str(data / "flows.json"))
        run("linker.py", "--aws", str(data / "aws.json"), "--code", str(data / "code.json"), "--overrides", str(overrides),
            "--out", str(data / "links.json"))
        run("rules.py", "--data", str(data), "--overrides", str(overrides))
        for f in SITE_FILES:
            text = (data / f).read_text(encoding="utf-8").replace(str(repo), "/home/demo/kestrel").replace(tmp, "/home/demo")
            (OUT / f).write_text(text, encoding="utf-8")
    print(f"demo data written to {OUT}")


if __name__ == "__main__":
    main()
