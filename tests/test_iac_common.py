# tests/test_iac_common.py
import json

import iac_common as c


def test_empty_inventory_has_every_collector_key() -> None:
    inv = c.empty_inventory("eu-west-1")
    assert inv["mode"] == "design" and inv["region"] == "eu-west-1"
    for k in ("ecs", "ec2", "rds", "lb", "sqs", "lambda", "cost", "sns", "cache", "search", "apigw", "cdn", "cognito", "dynamodb", "ses", "eventbridge"):
        assert inv[k] == []
    assert inv["s3"] == {"count": 0} and inv["eips"] == {"total": 0, "unattached": 0}


def test_ecs_row_counts_planned_tasks_as_running() -> None:
    r = c.ecs_row("shop", "api", desired=2, images=["shop/api:1"], env_vars={"DB": "postgres://db/x"}, build="api", build_from="compose.yml", source="compose")
    assert r["running"] == 2 and r["desired"] == 2 and r["ownImage"] is True
    assert r["refs"] == [{"k": "DB", "v": "db"}]
    assert r["cpu"] == 0 and r["memMax"] == 0 and r["env"] == "prod"


def test_public_image_without_build_is_third_party() -> None:
    assert c.ecs_row("x", "pgadmin", images=["dpage/pgadmin4:8"])["ownImage"] is False
    assert c.ecs_row("x", "api", images=["123.dkr.ecr.us-east-1.amazonaws.com/api:1"])["ownImage"] is True


def test_env_from_name() -> None:
    assert c.env_of("staging-shop/api") == "staging"
    assert c.env_of("prod-shop/api") == "prod"


def test_clean_name_drops_interpolation() -> None:
    assert c.clean_name("${var.env}-orders", "fallback") == "orders"
    assert c.clean_name("${var.name}", "fallback") == "fallback"


def test_infra_kind_of_image() -> None:
    assert c.infra_kind("postgres:16") == "rds"
    assert c.infra_kind("bitnami/redis:7") == "cache"
    assert c.infra_kind("opensearchproject/opensearch:2") == "search"
    assert c.infra_kind("node:20") is None


def test_connection_string_keeps_only_the_host() -> None:
    row = c.ecs_row("x", "api", env_vars={"DATABASE_URL": "postgres://app:Sup3rS3cret@db:5432/shop?sslmode=require"})
    assert row["refs"] == [{"k": "DATABASE_URL", "v": "db"}]
    assert "Sup3rS3cret" not in json.dumps(row)


def test_only_allowlisted_values_survive() -> None:
    leaks = {"API_KEY": "abc12345", "DB_PASSWORD": "hunter2", "DB_PASS": "hunter2", "PASS": "hunter2", "MYSQL_ROOT_PW": "hunter2",
             "STRIPE_KEY": "sk_live_abc", "GITHUB_PAT": "ghp_abcdefghijklmnopqrstuvwxyzABCDEFGHIJ", "SLACK": "xoxb-123-456-abc",
             "PGPASS": "hunter2", "PASSPHRASE": "correct-horse", "SESSION_NAME": "web", "QUEUE_TOKEN": "kestrel",
             "BUILD_ID": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", "SERVICE_ID": "123e4567-e89b-12d3-a456-426614174000",
             "SERVICE_TAG": "k3m9q2w7z1abk3m9q2w7z1ab", "REDIS_URL": "redis://:p/a@cache:6379/0"}
    row = c.ecs_row("x", "api", env_vars={**leaks, "ORDERS_QUEUE": "kestrel-orders.fifo", "DB_HOST": "db", "CACHE_HOST": "cache:6379"})
    assert {r["k"]: r["v"] for r in row["refs"]} == {"ORDERS_QUEUE": "kestrel-orders.fifo", "DB_HOST": "db", "CACHE_HOST": "cache:6379"}


def test_url_values_keep_only_a_clean_host() -> None:
    row = c.ecs_row("x", "api", env_vars={"REDIS_URL": "redis://cache:6379", "A_URL": "https://u:p@api.internal:8443/x?y=1#z", "B_URL": "http://[::1]/"})
    assert {r["k"]: r["v"] for r in row["refs"]} == {"REDIS_URL": "cache", "A_URL": "api.internal"}


def test_design_mode_does_not_import_boto3() -> None:
    import subprocess
    import sys
    code = "import sys; import iac_common as c; c.safe_ref('A_URL','x://h'); print('boto3' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={"PYTHONPATH": str(__import__("pathlib").Path(c.__file__).parent)})
    assert out.stdout.strip() == "False"


def test_aws_values_are_masked() -> None:
    row = c.ecs_row("x", "api", env_vars={"Q": "https://sqs.us-east-1.amazonaws.com/123456789012/kestrel-orders",
                                          "DB": "app-db.k3m9q2w7z1ab.eu-west-1.rds.amazonaws.com", "ACCT_HOST": "acct-123456789012.io"})
    refs = {r["k"]: r["v"] for r in row["refs"]}
    assert refs["Q"] == "https://sqs.us-east-1.amazonaws.com/************/kestrel-orders"
    assert refs["DB"] == "app-db.xxxxxxxx.eu-west-1.rds.amazonaws.com"
    assert refs["ACCT_HOST"] == "acct-************.io"


def test_infra_kind_is_anchored_to_the_repository_name() -> None:
    assert c.infra_kind("bitnami/postgresql:16") == "rds" and c.infra_kind("localhost:5000/library/mongo@sha256:ab") == "rds"
    for image in ("prometheuscommunity/postgres-exporter", "mongo-express", "rediscommander/redis-commander", "node:20"):
        assert c.infra_kind(image) is None
    assert c.image_engine("bitnami/postgresql:16") == "postgresql"


def test_int_falls_back_on_anything_unreadable() -> None:
    assert c.as_int("3", 1) == 3 and c.as_int(0, 1) == 0 and c.as_int(None, 1) == 1
    assert c.as_int({"Ref": "Count"}, 2) == 2 and c.as_int("${var.n}", 2) == 2 and c.as_int(True, 2) == 2


def test_merge_keeps_same_named_rows_from_different_sources() -> None:
    inv = c.empty_inventory("d")
    a, b = c.rds_row("db", "postgres", endpoint="db"), c.rds_row("db", "mysql", endpoint="db")
    a["src"], b["src"] = "a/compose.yml", "b/compose.yml"
    c.merge(inv, {"rds": [a]})
    c.merge(inv, {"rds": [b]})
    assert len(inv["rds"]) == 2


def test_loose_values_are_dropped() -> None:
    leaks = {"SERVICE_ID": "sk-proj-abc", "SERVICE_NAME": "ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ENDPOINT": "xoxb-123-456-abc",
             "DB_HOST": "admin:1234", "DATABASE_URL": "hunter2", "API_HOST": "correcthorsebatterystaple", "STORE_URL": "kestrel"}
    assert c.ecs_row("x", "api", env_vars=leaks)["refs"] == []


def test_resource_names_and_hosts_survive() -> None:
    row = c.ecs_row("x", "api", env_vars={"ORDERS_QUEUE": "kestrel-orders.fifo", "DB_HOST": "db", "REDIS_URL": "redis://cache:6379",
                                          "SITE_URL": "api.kestrel.example", "CACHE_ADDR": "cache:6379", "A_QUEUE": "${}-orders"})
    assert {r["k"]: r["v"] for r in row["refs"]} == {"ORDERS_QUEUE": "kestrel-orders.fifo", "DB_HOST": "db", "REDIS_URL": "cache",
                                                     "SITE_URL": "api.kestrel.example", "CACHE_ADDR": "cache:6379", "A_QUEUE": "orders"}


def test_more_datastore_images() -> None:
    assert c.infra_kind("timescale/timescaledb:2") == "rds"
    assert c.infra_kind("redis/redis-stack:latest") == "cache"


def test_more_leaks_are_dropped() -> None:
    leaks = {"DB_HOST": "hunter2", "TABLE": "hunter2", "BUCKET": "pass123", "QUEUE": "Winter2024", "DB_NAME": "hunter2", "STREAM": "sk-abc",
             "MAIL_ADDR": "p4ss.w0rd", "LEARNED_PHRASE": "correcthorsebattery", "HOST_B": "sk-live", "LEARNED": "x", "UNLEARN_ARN": "hunter2"}
    assert c.ecs_row("x", "api", env_vars=leaks)["refs"] == []


def test_arn_key_only_on_a_word_boundary() -> None:
    row = c.ecs_row("x", "api", env_vars={"TOPIC_ARN": "orders-topic", "LEARNED": "abc"})
    assert row["refs"] == [{"k": "TOPIC_ARN", "v": "orders-topic"}]
