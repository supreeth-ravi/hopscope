# tests/test_iac_scan.py
import json
from pathlib import Path

import iac_scan


def make_repo(tmp: Path) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "shop-api").mkdir()
    (tmp / "shop-api" / "main.py").write_text("x = 1\n")
    (tmp / "docker-compose.yml").write_text("services:\n  shop-api:\n    build: ./shop-api\n    ports: ['8000:8000']\n  db:\n    image: postgres:16\n")
    (tmp / "infra").mkdir()
    (tmp / "infra" / "queues.tf").write_text('resource "aws_sqs_queue" "o" { name = "orders" }\n')
    (tmp / "node_modules" / "x").mkdir(parents=True)
    (tmp / "node_modules" / "x" / "docker-compose.yml").write_text("services:\n  junk: {image: node}\n")
    return tmp


def test_scan_merges_every_reader_and_skips_vendored(tmp_path: Path) -> None:
    inv = iac_scan.scan(make_repo(tmp_path), "design")
    assert inv["mode"] == "design"
    assert [r["n"] for r in inv["ecs"]] == ["shop-api"] and inv["ecs"][0]["build"] == "shop-api"
    assert [q["n"] for q in inv["sqs"]] == ["orders"] and [d["n"] for d in inv["rds"]] == ["db"]
    assert sorted(inv["sources"]) == ["docker-compose.yml", "infra/queues.tf"]


def test_cli_writes_aws_json(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    code = tmp_path / "code.json"
    code.write_text(json.dumps({"base": str(repo)}))
    out = tmp_path / "aws.json"
    iac_scan.main(["--code", str(code), "--out", str(out)])
    assert json.loads(out.read_text())["ecs"][0]["n"] == "shop-api"


def test_no_files_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    code = tmp_path / "code.json"
    code.write_text(json.dumps({"base": str(tmp_path)}))
    try:
        iac_scan.main(["--code", str(code), "--out", str(tmp_path / "aws.json")])
    except SystemExit as e:
        assert "no infrastructure files" in str(e)
    else:
        raise AssertionError("expected SystemExit")


def test_terraform_files_in_one_folder_are_read_together(tmp_path: Path) -> None:
    infra = tmp_path / "infra"
    infra.mkdir()
    (infra / "a_cluster.tf").write_text('resource "aws_ecs_cluster" "main" { name = "prod-kestrel" }\n'
                                        'resource "aws_ecs_service" "api" {\n  name = "shop-api"\n  cluster = aws_ecs_cluster.main.id\n'
                                        '  task_definition = aws_ecs_task_definition.api.arn\n  load_balancer { target_group_arn = aws_lb_target_group.api.arn }\n}\n')
    (infra / "b_task.tf").write_text('resource "aws_ecs_task_definition" "api" {\n  container_definitions = jsonencode([{\n'
                                     '    name = "shop-api"\n    image = "x.dkr.ecr.us-east-1.amazonaws.com/api:1"\n'
                                     '    environment = [{ name = "DB_HOST", value = "db" }]\n  }])\n}\n')
    (infra / "c_lb.tf").write_text('resource "aws_lb_target_group" "api" { name = "api" }\nresource "aws_lb" "public" { name = "front" }\n'
                                   'resource "aws_lb_listener" "https" {\n  load_balancer_arn = aws_lb.public.arn\n'
                                   '  default_action { target_group_arn = aws_lb_target_group.api.arn }\n}\n')
    inv = iac_scan.scan(tmp_path, "design")
    (svc,) = inv["ecs"]
    assert svc["ownImage"] is True and {"k": "DB_HOST", "v": "db"} in svc["refs"]
    assert inv["lb"][0]["routes"][0]["services"] == ["prod-kestrel/shop-api"]
    assert sorted(inv["sources"]) == ["infra/a_cluster.tf", "infra/b_task.tf", "infra/c_lb.tf"]


def test_bad_files_are_skipped_and_reported(tmp_path: Path) -> None:
    make_repo(tmp_path)
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "docker-compose.yml").write_bytes(b"services:\n  a: [\xff\xfe\n")
    (tmp_path / "bad" / "t.yaml").write_text("Resources: {A: {Type: AWS::SQS::Queue, Properties: {QueueName: [1}}}\n")
    (tmp_path / "link.yml").symlink_to(tmp_path / "docker-compose.yml")
    (tmp_path / "plain.yaml").write_text("name: nothing\n")
    inv = iac_scan.scan(tmp_path, "design")
    assert [r["n"] for r in inv["ecs"]] == ["shop-api"] and "link.yml" not in inv["sources"]
    assert all(set(s) == {"file", "error"} for s in inv["skipped"])


def test_reader_exceptions_become_skips(tmp_path: Path, monkeypatch) -> None:
    make_repo(tmp_path)

    def boom(text: str, rel: str) -> dict:
        raise RuntimeError("x")

    monkeypatch.setattr(iac_scan.iac_terraform, "read", boom)
    inv = iac_scan.scan(tmp_path, "design")
    assert inv["skipped"] == [{"file": "infra/queues.tf", "error": "RuntimeError"}] and inv["ecs"]


def test_out_directory_is_created(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    code = tmp_path / "code.json"
    code.write_text(json.dumps({"base": str(repo)}))
    iac_scan.main(["--code", str(code), "--out", str(tmp_path / "new" / "data" / "aws.json")])
    assert (tmp_path / "new" / "data" / "aws.json").exists()


def test_error_lists_skipped_files(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "docker-compose.yml").write_text("services:\n  a: {image: x}\n")
    monkeypatch.setattr(iac_scan.iac_compose, "read", lambda text, rel: 1 / 0)
    code = tmp_path / "code.json"
    code.write_text(json.dumps({"base": str(tmp_path)}))
    try:
        iac_scan.main(["--code", str(code), "--out", str(tmp_path / "aws.json")])
    except SystemExit as e:
        assert "no infrastructure files" in str(e) and "docker-compose.yml" in str(e)
    else:
        raise AssertionError("expected SystemExit")
