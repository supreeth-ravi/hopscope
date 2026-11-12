"""Golden code maps: the scanner's output for the demo codebase and a mixed Python/JS/Go repo must not change
when the scanner is reorganised. Regenerate on purpose with HOPSCOPE_REGEN_GOLDEN=1."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "hopscope" / "scripts" / "code_scan.py"
GOLDEN = Path(__file__).resolve().parent / "golden"

MIXED = {
    "api/Dockerfile": "FROM python:3.12\n",
    "api/app/__init__.py": "",
    "api/app/main.py": ("from fastapi import FastAPI\nfrom .routes import orders, beans\nfrom app.web import pages\n"
                        "app = FastAPI()\napp.include_router(orders.router, prefix='/orders')\napp.include_router(beans.router, prefix='/beans')\n"),
    "api/app/routes/__init__.py": "",
    "api/app/routes/orders.py": ("import boto3\nfrom fastapi import APIRouter\nfrom ..db import session\nrouter = APIRouter()\n\n"
                                 "@router.get('/{order_id}')\nasync def get_order(order_id: int):\n    '''Look up one order.'''\n    return session.get(order_id)\n\n"
                                 "@router.api_route('/bulk', methods=['POST', 'PUT'])\ndef bulk_orders():\n    boto3.client('sqs').send_message(QueueUrl='q')\n\n"
                                 "@router.websocket('/live')\nasync def live(ws):\n    pass\n"),
    "api/app/routes/beans.py": ("from fastapi import APIRouter\nimport redis\nrouter = APIRouter(prefix='/v2')\n\n"
                                "@router.get('')\ndef list_beans():\n    return []\n"),
    "api/app/db.py": "from sqlalchemy.orm import Session\nsession = Session()\n",
    "api/app/web/__init__.py": "",
    "api/app/web/pages.py": ("from flask import Blueprint\nimport openai\nbp = Blueprint('pages', __name__)\n\n"
                             "@bp.route('/about', methods=['GET', 'HEAD'])\ndef about():\n    return 'ok'\n"),
    "api/app/broken.py": "def oops(:\n",
    "web/package.json": "{}",
    "web/src/server.ts": ("import { price } from './lib/price';\nconst u = require('./util');\nimport('./lazy');\n"
                          "import express from 'express';\nimport { Pool } from 'pg';\nrouter.get('/health', h);\napp.post(\"/cart\", add);\napp.all('/any', a);\n"),
    "web/src/lib/price.ts": "export const price = 1;\n",
    "web/src/util.js": "module.exports = {};\n",
    "web/src/lazy/index.ts": "export default 1;\n",
    "web/src/orders.controller.ts": ("import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';\n@Controller('orders')\nexport class OrdersController {\n"
                                     "  @Get(':id')\n  async findOne() {}\n  @Post()\n  create() {}\n}\n"),
    "web/src/thing.test.ts": "router.get('/test-only', h);\n",
    "go/go.mod": "module example.com/roast\n",
    "go/main.go": ('package main\nimport "github.com/jackc/pgx/v5"\nfunc main() {\n  r.GET("/roasts", list)\n  http.HandleFunc("/healthz", hz)\n'
                   '  r.POST("relative", x)\n}\n'),
}


def _load_demo():
    spec = importlib.util.spec_from_file_location("make_demo", ROOT / "docs" / "demo" / "make_demo.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(base: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _scan(repo: Path, out: Path, script: Path = SCRIPT) -> dict:
    subprocess.run([sys.executable, str(script), "--repo", str(repo), "--no-floorplan", "--out", str(out)], check=True, capture_output=True)
    data = json.loads(out.read_text())
    data.pop("generatedAt")
    data["base"] = "<base>"
    return canonical(data)


def canonical(data: dict) -> dict:
    """File order follows the filesystem's listing order, which differs between machines: sort files by path and
    renumber everything that points at them. Routes keep their order within a file (flows.json refers to routes by
    index), and client calls point at routes by their new index."""
    order = sorted(range(len(data["files"])), key=lambda i: data["files"][i]["p"])
    new = {old: i for i, old in enumerate(order)}
    data["files"] = [data["files"][i] for i in order]
    routes = sorted(enumerate(data["routes"]), key=lambda x: new[x[1]["f"]])  # stable: in-file order kept
    route_new = {old: i for i, (old, _) in enumerate(routes)}
    data["routes"] = [{**r, "f": new[r["f"]]} for _, r in routes]
    data["links"] = sorted([new[a], new[b], k, lab] for a, b, k, lab in data["links"])
    data["clientCalls"] = sorted([new[c[0]], *c[1:4], route_new.get(c[4]) if c[4] is not None else None, *c[5:]]
                                 for c in data.get("clientCalls", []))
    return data


def _compare(name: str, got: dict) -> None:
    path = GOLDEN / name
    if os.environ.get("HOPSCOPE_REGEN_GOLDEN"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    assert got == json.loads(path.read_text())


EXPRESS = {
    "shop/package.json": '{"dependencies": {"express": "4"}}',
    "shop/src/app.js": ("const express = require('express');\nimport cups from './routes/cups';\nconst { beans } = require('./routes/beans');\n"
                        "app.use('/api/v1/cups', cups);\napp.use('/api/v2/cups', cups);\napp.use('/api/v1', require('./routes/beans'));\n"
                        "app.get('/health', (req, res) => res.send('ok'));\n"),
    "shop/src/routes/cups.js": "const router = express.Router();\nrouter.get('/', list);\nrouter.post('/:id/fill', fill);\n",
    "shop/src/routes/beans.js": "const router = express.Router();\nrouter.get('/beans', list);\n",
    "till/package.json": '{"dependencies": {"react": "18", "axios": "1"}}',
    "till/src/api.ts": ("const shop = axios.create({ baseURL: `${API_URL}/api/v1` });\nexport const listCups = () => shop.get('/cups');\n"
                        "export const fill = (id: number) => shop.post(`/cups/${id}/fill${qs}`);\nexport const beans = () => fetch('/api/v1/beans?all=1');\n"
                        "export const gone = () => fetch('/api/v1/teas');\n"),
}


@pytest.mark.parametrize("name", ["demo", "mixed", "express"])
def test_code_map_matches_golden(tmp_path: Path, name: str) -> None:
    repo = tmp_path / "repo"
    if name == "demo":
        _load_demo().write_codebase(repo)
    else:
        _write(repo, MIXED if name == "mixed" else EXPRESS)
    _compare(f"{name}_code.json", _scan(repo, tmp_path / "code.json"))
