# tests/test_design_mode.py
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "hopscope" / "scripts"


def make_shop(root: Path) -> None:
    (root / "shop-api" / "app").mkdir(parents=True)
    (root / "shop-api" / "app" / "main.py").write_text(
        "import os\nfrom fastapi import FastAPI\nimport boto3\napp = FastAPI()\nsqs = boto3.client('sqs')\n\n"
        "@app.post('/orders')\ndef create_order():\n    sqs.send_message(QueueUrl=os.environ['ORDERS_QUEUE'], MessageBody='x')\n    return {}\n")
    (root / "shop-api" / "requirements.txt").write_text("fastapi\nboto3\n")
    (root / "worker").mkdir()
    (root / "worker" / "main.py").write_text("import boto3\nsqs = boto3.client('sqs')\nwhile True:\n    sqs.receive_message(QueueUrl='orders')\n")
    (root / "worker" / "requirements.txt").write_text("boto3\n")
    (root / "docker-compose.yml").write_text(
        "services:\n  shop-api:\n    build: ./shop-api\n    ports: ['8000:8000']\n    environment: {DATABASE_URL: 'postgres://app@db/shop', ORDERS_QUEUE: orders}\n"
        "  order-worker:\n    build: ./worker\n  db:\n    image: postgres:16\n")
    (root / "queues.tf").write_text('resource "aws_sqs_queue" "orders" { name = "orders" }\n')


def test_design_builds_a_site_with_no_aws(tmp_path: Path) -> None:
    repo, work = tmp_path / "shop", tmp_path / "work"
    make_shop(repo)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "AWS_CONFIG_FILE": "/nonexistent", "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent"}
    # sitecustomize runs in every Python child of the pipeline: importing boto3 anywhere fails the run.
    (tmp_path / "sitecustomize.py").write_text(
        "import sys\n\nclass Block:\n    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('boto3', 'botocore'):\n            raise ImportError('boto3 imported in design mode')\n\n"
        "sys.meta_path.insert(0, Block())\n")
    env["PYTHONPATH"] = str(tmp_path)
    blocked = subprocess.run([sys.executable, "-c", "import boto3"], capture_output=True, text=True, env=env)
    assert blocked.returncode != 0 and "design mode" in blocked.stderr  # the guard itself works
    res = subprocess.run([sys.executable, str(SCRIPTS / "hopscope.py"), "design", "--repo", str(repo), "--workdir", str(work), "--no-floorplan"],
                         capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    model = json.loads((work / "site" / "model.json").read_text())
    links = json.loads((work / "data" / "links.json").read_text())
    assert model["meta"]["mode"] == "design"
    assert links["placement"]["compose/shop-api"]["confidence"] == 1.0
    assert links["placement"]["compose/order-worker"]["code"] == "worker"
    assert "orders" in links["queues"]["shop-api"]
    assert (work / "site" / "index.html").exists()
    assert model["meta"]["mode"] == "design" and json.loads((work / "data" / "aws.json").read_text())["lb"][0]["n"] == "compose"


def test_design_removes_a_stale_site_ts_json(tmp_path: Path) -> None:
    repo, work = tmp_path / "shop", tmp_path / "work"
    make_shop(repo)
    (work / "site").mkdir(parents=True)
    (work / "site" / "ts.json").write_text("{}")
    res = subprocess.run([sys.executable, str(SCRIPTS / "hopscope.py"), "design", "--repo", str(repo), "--workdir", str(work), "--no-floorplan"],
                         capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert res.returncode == 0, res.stdout + res.stderr
    assert not (work / "site" / "ts.json").exists()


def test_design_output_never_carries_an_account_number(tmp_path: Path) -> None:
    import re
    repo, work = tmp_path / "shop", tmp_path / "work"
    make_shop(repo)
    ecr = "123456789012.dkr.ecr.us-east-1.amazonaws.com/kestrel/shop-api:1.4"
    (repo / "shop-api" / "deploy.yml").write_text(f"image: {ecr}\n")
    (repo / "docker-compose.prod.yml").write_text(f"services:\n  shop-api:\n    image: {ecr}\n    ports: ['443:8000']\n")
    res = subprocess.run([sys.executable, str(SCRIPTS / "hopscope.py"), "design", "--repo", str(repo), "--workdir", str(work), "--no-floorplan"],
                         capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert res.returncode == 0, res.stdout + res.stderr
    for f in [*(work / "data").glob("*.json"), *(work / "site").glob("*.json")]:
        assert not re.search(r"[0-9]{12}", f.read_text()), f.name
