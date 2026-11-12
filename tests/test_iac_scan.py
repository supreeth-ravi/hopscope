# tests/test_iac_scan.py
import json
import re
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


def test_optional_scan_with_no_files_removes_a_stale_inventory(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    code = tmp_path / "code.json"
    code.write_text(json.dumps({"base": str(tmp_path)}))
    out = tmp_path / "iac.json"
    out.write_text("{}")
    iac_scan.main(["--code", str(code), "--out", str(out), "--optional"])
    assert not out.exists()


def test_terraform_rows_say_whether_the_desired_count_is_literal(tmp_path: Path) -> None:
    infra = tmp_path / "infra"
    infra.mkdir()
    (infra / "main.tf").write_text('''resource "aws_ecs_task_definition" "api" {
  family = "shop-api"
}
resource "aws_ecs_service" "api" {
  name            = "api"
  desired_count   = 3
  task_definition = aws_ecs_task_definition.api.arn
}
resource "aws_ecs_service" "worker" {
  name          = "worker"
  desired_count = var.workers
}
''')
    inv = iac_scan.scan(tmp_path, "design")
    rows = {r["n"]: r for r in inv["ecs"]}
    assert rows["api"]["desiredSet"] is True and rows["api"]["desired"] == 3 and rows["api"]["taskFamily"] == "shop-api"
    assert rows["worker"]["desiredSet"] is False and rows["worker"]["taskFamily"] == ""
    assert rows["api"]["nameVar"] is False


def test_terraform_rows_say_when_a_name_came_from_a_variable(tmp_path: Path) -> None:
    (tmp_path / "q.tf").write_text('resource "aws_sqs_queue" "orders" {\n  name = "${var.env}-orders"\n}\nresource "aws_sqs_queue" "jobs" {\n  name = "jobs"\n}\n')
    rows = {r["n"]: r for r in iac_scan.scan(tmp_path, "design")["sqs"]}
    assert rows["orders"]["nameVar"] is True and rows["jobs"]["nameVar"] is False


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


def test_scan_masks_account_numbers_in_every_row(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  roaster-123456789012:\n    image: 123456789012.dkr.ecr.us-east-1.amazonaws.com/kestrel/roaster:1\n    ports: ['80:80']\n")
    inv = iac_scan.scan(tmp_path, "design")
    assert inv["ecs"][0]["ownImage"] is True and inv["ecs"][0]["images"] == ["roaster"]
    assert not re.search(r"[0-9]{12}", json.dumps(inv))


def test_service_keys_are_unique_across_compose_files(tmp_path: Path) -> None:
    text = "services:\n  api: {build: ., ports: ['80:80']}\n"
    for rel in ("deploy/docker-compose.yml", "deploy/docker-compose.staging.yml", "kestrel/deploy/docker-compose.yml", "beans/deploy/docker-compose.yml"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    inv = iac_scan.scan(tmp_path, "design")
    keys = [f'{r["cluster"]}/{r["n"]}' for r in inv["ecs"]]
    assert len(keys) == 4 and len(set(keys)) == 4 and "deploy-staging/api" in keys
    routed = sorted(s for lb in inv["lb"] for r in lb["routes"] for s in r["services"])
    assert routed == sorted(keys) and len({lb["n"] for lb in inv["lb"]}) == 4


def test_same_named_datastores_get_unique_display_names(tmp_path: Path) -> None:
    text = "services:\n  postgres: {image: postgres:16}\n  redis: {image: redis:7}\n  search: {image: opensearchproject/opensearch:2}\n"
    for rel in ("beans/docker-compose.yml", "cups/docker-compose.yml", "cups/docker-compose.staging.yml"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    inv = iac_scan.scan(tmp_path, "design")
    assert sorted(d["n"] for d in inv["rds"]) == ["postgres", "postgres (cups)", "postgres (cups-staging)"]
    assert sorted(d["n"] for d in inv["cache"]) == ["redis", "redis (cups)", "redis (cups-staging)"]
    assert sorted(d["n"] for d in inv["search"]) == ["search", "search (cups)", "search (cups-staging)"]
    assert {d["endpoint"] for k in ("rds", "cache", "search") for d in inv[k]} == {"postgres", "redis", "search"}
