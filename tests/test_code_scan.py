from pathlib import Path

import pytest

import code_scan


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    api = tmp_path / "backend"
    (api / "app" / "routes").mkdir(parents=True)
    (api / "Dockerfile").write_text("FROM python:3.12\n")
    (api / "app" / "__init__.py").write_text("")
    (api / "app" / "routes" / "__init__.py").write_text("")
    (api / "app" / "main.py").write_text(
        "from fastapi import FastAPI\nfrom app.routes import auth\napp = FastAPI()\napp.include_router(auth.router, prefix='/auth')\n")
    (api / "app" / "routes" / "auth.py").write_text(
        "import boto3\nfrom fastapi import APIRouter\nfrom sqlalchemy import select\nrouter = APIRouter()\n"
        "@router.post('/login')\nasync def login():\n    '''Sign a user in.'''\n    boto3.client('cognito-idp')\n")
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "package.json").write_text("{}")
    (web / "src" / "server.ts").write_text("import { x } from './util';\nrouter.get('/health', h);\napp.post(\"/orders\", create);\n")
    (web / "src" / "util.ts").write_text("export const x = 1;\n")
    (web / "src" / "orders.controller.ts").write_text("@Controller('orders')\nclass C {\n  @Get(':id')\n  findOne() {}\n}\n")
    return tmp_path


def test_fastapi_prefix_is_applied(repo: Path) -> None:
    cm = code_scan.scan([repo])
    paths = {(r["m"], r["p"]) for r in cm.routes}
    assert ("POST", "/auth/login") in paths


def test_js_and_nest_routes(repo: Path) -> None:
    cm = code_scan.scan([repo])
    paths = {(r["m"], r["p"]) for r in cm.routes}
    assert {("GET", "/health"), ("POST", "/orders"), ("GET", "/orders/:id")} <= paths


def test_services_detected_from_manifests(repo: Path) -> None:
    cm = code_scan.scan([repo])
    assert {"backend", "web"} <= set(cm.services)


def test_externals_detected(repo: Path) -> None:
    cm = code_scan.scan([repo])
    auth = next(f for f in cm.files if f["p"].endswith("routes/auth.py"))
    assert {"postgres", "cognito"} <= set(auth["x"])


def test_js_relative_import_link(repo: Path) -> None:
    cm = code_scan.scan([repo])
    idx = {f["p"]: i for i, f in enumerate(cm.files)}
    assert [idx["web/src/server.ts"], idx["web/src/util.ts"], 0, ""] in cm.links
