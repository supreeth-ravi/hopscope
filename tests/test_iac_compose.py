# tests/test_iac_compose.py
import iac_compose

COMPOSE = """
services:
  shop-api:
    build: ./shop-api
    environment:
      DATABASE_URL: postgres://app@db:5432/shop
      ORDERS_QUEUE_URL: https://sqs.us-east-1.amazonaws.com/1/kestrel-orders.fifo
    ports: ["8000:8000"]
    deploy: {replicas: 2}
  order-worker:
    build: {context: ./workers/order, dockerfile: Dockerfile}
    environment: ["REDIS_URL=redis://cache:6379"]
  db:
    image: postgres:16
  cache:
    image: redis:7
  mailhog:
    image: mailhog/mailhog
"""


def test_services_become_ecs_rows_with_build_folders() -> None:
    part = iac_compose.read(COMPOSE, "deploy/docker-compose.yml")
    rows = {r["n"]: r for r in part["ecs"]}
    assert rows["shop-api"]["build"] == "deploy/shop-api"
    assert rows["shop-api"]["desired"] == 2 and rows["shop-api"]["cluster"] == "deploy"
    assert rows["order-worker"]["build"] == "deploy/workers/order"
    assert {"k": "REDIS_URL", "v": "cache"} in rows["order-worker"]["refs"]
    assert rows["mailhog"]["ownImage"] is False


def test_datastores_become_data_rows() -> None:
    part = iac_compose.read(COMPOSE, "docker-compose.yml")
    assert [d["n"] for d in part["rds"]] == ["db"] and part["rds"][0]["endpoint"] == "db"
    assert [c["n"] for c in part["cache"]] == ["cache"]
    assert "db" not in {r["n"] for r in part["ecs"]}


def test_published_ports_become_entry_routes() -> None:
    part = iac_compose.read(COMPOSE, "docker-compose.yml")
    lb = part["lb"][0]
    assert lb["n"] == "compose" and lb["source"] == "compose" and lb["routes"][0]["services"] == ["compose/shop-api"]
    assert lb["routes"][0]["when"] == "port 8000" and lb["routes"][0]["port"] == 8000


def test_not_a_compose_file() -> None:
    assert iac_compose.read("name: x\n", "docker-compose.yml")["ecs"] == []


def test_ports_in_every_syntax_and_ranges() -> None:
    text = """
services:
  a: {build: ., ports: ["80"]}
  b: {build: ., ports: [8080]}
  c: {build: ., expose: ["9000"]}
  d: {build: ., ports: [{target: 7000}]}
  e: {build: ., ports: ["3000-3005:3000-3005"]}
  f: {build: ., ports: [{target: 1, published: "4000", protocol: tcp}]}
"""
    lbs = iac_compose.read(text, "docker-compose.yml")["lb"]
    routes = {r["services"][0].split("/")[1]: r["port"] for r in lbs[0]["routes"]}
    assert routes == {"a": 80, "b": 8080, "d": 7000, "e": 3000, "f": 4000}
    assert [r["port"] for r in lbs[1]["routes"]] == [9000]


def test_unsafe_build_contexts_are_not_folders() -> None:
    text = "services:\n" + "".join(f"  s{i}: {{build: '{b}'}}\n" for i, b in enumerate(["/abs", "https://x/y.git", "${DIR}", "../up", ".."]))
    assert [r["build"] for r in iac_compose.read(text, "docker-compose.yml")["ecs"]] == [None] * 5


def test_look_alike_images_are_services_not_datastores() -> None:
    text = "services:\n  x: {image: prometheuscommunity/postgres-exporter}\n  y: {image: mongo-express}\n  z: {image: bitnami/postgresql:16}\n"
    part = iac_compose.read(text, "docker-compose.yml")
    assert [r["n"] for r in part["ecs"]] == ["x", "y"] and part["rds"][0]["engine"] == "postgresql"


def test_broken_yaml_replicas_and_multi_doc() -> None:
    assert iac_compose.read("services: [unclosed\n", "docker-compose.yml")["ecs"] == []
    text = "---\nname: x\n---\nservices:\n  a: {build: ., deploy: {replicas: 0}}\n  b: {build: ., deploy: {replicas: '${N}'}}\n"
    assert [(r["n"], r["desired"]) for r in iac_compose.read(text, "docker-compose.yml")["ecs"]] == [("a", 0), ("b", 1)]


def test_same_named_datastores_in_two_compose_files_both_survive() -> None:
    import iac_common
    inv = iac_common.empty_inventory("d")
    for rel in ("a/docker-compose.yml", "b/docker-compose.yml"):
        iac_common.merge(inv, iac_compose.read("services:\n  db: {image: postgres:16}\n  api: {build: ., ports: ['80:80']}\n", rel))
    assert len(inv["rds"]) == 2 and len(inv["lb"]) == 2 and len(inv["ecs"]) == 2


def test_expose_ports_route_on_an_internal_lb() -> None:
    part = iac_compose.read("services:\n  api:\n    build: ./api\n    ports: ['80:8000']\n  worker:\n    build: ./w\n    expose: ['9000']\n", "docker-compose.yml")
    by = {lb["n"]: lb for lb in part["lb"]}
    assert by["compose"]["scheme"] == "internet-facing" and [r["port"] for r in by["compose"]["routes"]] == [80]
    assert by["compose (internal)"]["scheme"] == "internal" and by["compose (internal)"]["routes"][0]["services"] == ["compose/worker"]


def test_host_values_must_name_a_service_in_the_same_file() -> None:
    text = "services:\n  db1:\n    image: postgres:16\n  api:\n    build: ./api\n    environment: {DB_HOST: db1, CACHE_HOST: hunter2, A_HOST: 'db1:5432', DB_URL: db1}\n"
    refs = {r["k"]: r["v"] for r in iac_compose.read(text, "docker-compose.yml")["ecs"][0]["refs"]}
    assert refs == {"DB_HOST": "db1", "A_HOST": "db1:5432"}


def test_known_host_ports_must_be_declared_or_well_known() -> None:
    text = ("services:\n  db1:\n    image: postgres:16\n  admin:\n    image: x\n  api:\n    image: y\n    expose: ['7777']\n"
            "  web:\n    build: .\n    environment: {A_HOST: 'db1:5432', B_HOST: 'admin:1234', C_HOST: 'admin', D_HOST: 'api:7777', E_HOST: 'api:1234'}\n")
    refs = {r["k"]: r["v"] for r in next(x for x in iac_compose.read(text, "docker-compose.yml")["ecs"] if x["n"] == "web")["refs"]}
    assert refs == {"A_HOST": "db1:5432", "D_HOST": "api:7777"}


def test_named_cluster_lb_names() -> None:
    part = iac_compose.read("services:\n  api: {build: ., ports: ['80:80'], expose: ['81']}\n", "deploy/docker-compose.yml")
    assert [lb["n"] for lb in part["lb"]] == ["deploy (compose)", "deploy (compose, internal)"]


def test_build_folder_follows_a_dockerfile_below_the_context() -> None:
    text = """
services:
  roaster: {build: {context: ../.., dockerfile: roaster/Dockerfile}}
  admin: {build: {context: ., dockerfile: admin/Dockerfile.backend}}
  beans: {build: {context: ./beans, dockerfile: Dockerfile}}
  cups: {build: {context: ./cups, dockerfile: ../shared/Dockerfile}}
  escaped: {build: {context: ../../.., dockerfile: kestrel/roaster/Dockerfile}}
  odd: {build: {context: ., dockerfile: /abs/Dockerfile}}
"""
    rows = {r["n"]: r["build"] for r in iac_compose.read(text, "deploy/local/docker-compose.yml")["ecs"]}
    assert rows == {"roaster": "roaster", "admin": "deploy/local/admin", "beans": "deploy/local/beans", "cups": "deploy/local/cups",
                    "escaped": None, "odd": "deploy/local"}


def test_cluster_names_carry_the_compose_file_variant() -> None:
    text = "services:\n  api: {build: ., ports: ['80:80'], expose: ['81']}\n  db: {image: postgres:16}\n"
    cases = {"deploy/docker-compose.yml": "deploy", "deploy/docker-compose.staging.yml": "deploy-staging",
             "deploy/compose.prod.yaml": "deploy-prod", "docker-compose.yml": "compose", "docker-compose.dev.yml": "compose-dev"}
    for rel, cluster in cases.items():
        part = iac_compose.read(text, rel)
        assert part["ecs"][0]["cluster"] == cluster, rel
    staging = iac_compose.read(text, "deploy/docker-compose.staging.yml")
    assert staging["ecs"][0]["env"] == "staging" and staging["rds"][0]["env"] == "staging"
    assert [lb["n"] for lb in staging["lb"]] == ["deploy-staging (compose)", "deploy-staging (compose, internal)"]
    assert staging["lb"][0]["routes"][0]["services"] == ["deploy-staging/api"]
    assert [lb["n"] for lb in iac_compose.read(text, "compose.dev.yaml")["lb"]] == ["compose-dev", "compose-dev (internal)"]
    assert iac_compose.read(text, "deploy/docker-compose.prod.yml")["ecs"][0]["env"] == "prod"


ENV_FILE = """# kestrel coffee local settings
DB_PASSWORD=hunter2-roast
export API_TOKEN="tok_9f8e7d6c5b4a39281706"
DATABASE_URL='postgres://app:s3cret-beans@db:5432/shop'
ORDERS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/123456789012/kestrel-orders
CACHE_HOST=old-cache
BEANS_TABLE=kestrel-beans # the bean catalogue
not a line
"""


def env_repo(tmp_path, env_file: str) -> str:
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / ".env").write_text(ENV_FILE)
    (tmp_path / "outside.env").write_text("ORDERS_QUEUE=leak\n")
    return ("services:\n  cache: {image: redis:7}\n  db: {image: postgres:16}\n  api:\n    build: .\n"
            f"    env_file: {env_file}\n    environment: {{CACHE_HOST: cache}}\n")


def test_env_file_values_go_through_the_allowlist(tmp_path) -> None:
    import json
    text = env_repo(tmp_path, "[.env, missing.env, {path: ../../outside.env, required: false}, /etc/passwd]")
    part = iac_compose.read(text, "deploy/docker-compose.yml", base=tmp_path)
    refs = {r["k"]: r["v"] for r in part["ecs"][0]["refs"]}
    assert refs == {"DATABASE_URL": "db", "ORDERS_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/************/kestrel-orders",
                    "CACHE_HOST": "cache", "BEANS_TABLE": "kestrel-beans"}
    dumped = json.dumps(part)
    for secret in ("hunter2", "tok_", "s3cret", "123456789012", "leak", "API_TOKEN", "DB_PASSWORD"):
        assert secret not in dumped


def test_env_file_string_and_long_syntax(tmp_path) -> None:
    for spec in (".env", "{path: .env, required: true}", "[{path: ./.env}]"):
        import shutil
        shutil.rmtree(tmp_path / "deploy", ignore_errors=True)
        (tmp_path / "outside.env").unlink(missing_ok=True)
        part = iac_compose.read(env_repo(tmp_path, spec), "deploy/docker-compose.yml", base=tmp_path)
        assert {"k": "BEANS_TABLE", "v": "kestrel-beans"} in part["ecs"][0]["refs"], spec


def test_env_file_is_ignored_without_a_base_and_capped(tmp_path) -> None:
    part = iac_compose.read(env_repo(tmp_path, ".env"), "deploy/docker-compose.yml")
    assert part["ecs"][0]["refs"] == [{"k": "CACHE_HOST", "v": "cache"}]
    (tmp_path / "deploy" / ".env").write_text("".join(f"X{i}=1\n" for i in range(600)) + "LATE_TABLE=late\n")
    part = iac_compose.read("services:\n  api:\n    build: .\n    env_file: .env\n", "deploy/docker-compose.yml", base=tmp_path)
    assert all(r["k"] != "LATE_TABLE" for r in part["ecs"][0]["refs"])
