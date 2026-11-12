"""code.json is data: every reader that opens a file named in it stays under the scanned base, never follows a symlink,
reads only regular files and skips oversized ones (langs/common.read_contained)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import linker
from langs import braceflow
from langs.common import MAX_FILE_BYTES, read_contained

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "hopscope" / "scripts"
SECRET = "kestrel-secret-orders.fifo"


@pytest.fixture()
def tree(tmp_path: Path) -> tuple[Path, Path]:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.py").write_text(f"Q = '{SECRET}'\n")
    base = tmp_path / "repo"
    (base / "svc").mkdir(parents=True)
    (base / "svc" / "ok.py").write_text("x = 1\n")
    (base / "svc" / "link.py").symlink_to(outside / "leak.py")
    (base / "svc" / "big.py").write_text("x" * (MAX_FILE_BYTES + 1))
    (base / "svc" / "dir.py").mkdir()
    return base, outside


def test_read_contained_rules(tree: tuple[Path, Path]) -> None:
    base, _ = tree
    root = base.resolve()
    assert read_contained(root, "svc/ok.py") == "x = 1\n"
    assert read_contained(root, "../outside/leak.py") is None
    assert read_contained(root, "svc/link.py") is None
    assert read_contained(root, "svc/big.py") is None
    assert read_contained(root, "svc/dir.py") is None
    assert read_contained(root, "svc/missing.py") is None
    assert read_contained(root, str(root.parent / "outside" / "leak.py")) is None  # absolute path
    assert read_contained(root, "svc/ok.py", limit=3) is None


def test_linker_queue_links_ignores_escaping_paths(tree: tuple[Path, Path]) -> None:
    base, _ = tree
    files = [{"p": "svc/../../outside/leak.py", "x": []}, {"p": "svc/link.py", "x": []}]
    inv = {"mode": "design", "ecs": [], "sqs": [{"n": SECRET, "sent": 0, "backlog": 0}]}
    queues, _ = linker.queue_links({"base": str(base), "files": files}, inv, ["svc"], {}, base)
    assert queues == {}
    (base / "svc" / "real.py").write_text(f"Q = '{SECRET}'\n")
    files.append({"p": "svc/real.py", "x": []})
    queues, _ = linker.queue_links({"base": str(base), "files": files}, inv, ["svc"], {}, base)
    assert queues == {"svc": [SECRET]}


def test_braceflow_service_skips_escaping_paths(tree: tuple[Path, Path]) -> None:
    base, outside = tree
    (outside / "Leak.java").write_text("class Leak { void a() {} }\n")
    (base / "svc" / "Link.java").symlink_to(outside / "Leak.java")
    svc = braceflow.Service(base, ["../outside/Leak.java", "svc/Link.java"], "postgres")
    assert svc.parsed == {} and svc.classes == {}


def test_call_flow_does_not_read_outside_base(tree: tuple[Path, Path], tmp_path: Path) -> None:
    base, outside = tree
    (outside / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n@app.get('/x')\ndef x():\n    db.execute('select 1')\n")
    (base / "svc" / "app.py").symlink_to(outside / "app.py")
    (base / "svc" / "real.py").write_text((outside / "app.py").read_text())
    files = [{"p": "../outside/app.py"}, {"p": "svc/app.py"}, {"p": "svc/real.py"}]
    data = {"base": str(base), "services": ["svc"], "files": files,
            "routes": [{"m": "GET", "p": "/x", "f": i, "l": 3} for i in range(3)]}
    (tmp_path / "code.json").write_text(json.dumps(data))
    subprocess.run([sys.executable, str(SCRIPTS / "call_flow.py"), str(tmp_path / "code.json"), str(tmp_path / "flows.json")],
                   check=True, capture_output=True)
    flows = json.loads((tmp_path / "flows.json").read_text())
    assert not flows.get("0") and not flows.get("1")
    assert flows["2"]  # the same handler inside the base is traced


def test_linker_deploy_files_skip_symlinks_and_escapes(tree: tuple[Path, Path]) -> None:
    base, outside = tree
    (outside / "Dockerfile").write_text("FROM kestrel-secret\n")
    (base / "svc" / "Dockerfile").symlink_to(outside / "Dockerfile")
    (base / "svc" / "deploy.sh").write_text("echo ok\n")
    files = dict(linker.deploy_files(base))
    assert "svc/Dockerfile" not in files and files.get("svc/deploy.sh") == "echo ok\n"
