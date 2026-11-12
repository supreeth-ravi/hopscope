# tests/test_iac_terraform.py
import iac_terraform

TF = '''
resource "aws_ecs_cluster" "main" {
  name = "prod-kestrel"
}

resource "aws_ecs_task_definition" "api" {
  family = "shop-api"
  container_definitions = jsonencode([{
    name  = "shop-api"
    image = "${aws_ecr_repository.api.repository_url}:latest"
    environment = [{ name = "ORDERS_QUEUE", value = "kestrel-orders.fifo" }]
  }])
}

resource "aws_ecs_service" "api" {
  name            = "shop-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 3
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "shop-api"
  }
}

resource "aws_lb" "public" {
  name     = "kestrel-public"
  internal = false
}

resource "aws_lb_target_group" "api" {
  name = "shop-api"
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.public.arn
  port              = 443
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_lb_listener_rule" "orders" {
  listener_arn = aws_lb_listener.https.arn
  condition {
    path_pattern { values = ["/orders/*"] }
  }
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_sqs_queue" "orders" {
  name           = "${var.prefix}kestrel-orders.fifo"
  redrive_policy = jsonencode({ deadLetterTargetArn = aws_sqs_queue.dlq.arn })
}

resource "aws_sqs_queue" "dlq" {
  name = "kestrel-orders-dlq.fifo"
}

resource "aws_rds_cluster" "db" {
  cluster_identifier = "kestrel-db"
  engine             = "aurora-postgresql"
}

resource "aws_lambda_function" "resize" {
  function_name = "image-resize"
  memory_size   = 1024
}

resource "aws_sns_topic" "alerts" { name = "ops-alerts" }
resource "aws_s3_bucket" "media" { bucket = "kestrel-media" }
'''


def test_ecs_service_with_cluster_image_env_and_count() -> None:
    part = iac_terraform.read(TF, "infra/main.tf")
    (svc,) = part["ecs"]
    assert (svc["cluster"], svc["n"], svc["desired"]) == ("prod-kestrel", "shop-api", 3)
    assert svc["ownImage"] is True and svc["source"] == "terraform"
    assert {"k": "ORDERS_QUEUE", "v": "kestrel-orders.fifo"} in svc["refs"]


def test_load_balancer_routes_reach_the_service() -> None:
    (lb,) = iac_terraform.read(TF, "infra/main.tf")["lb"]
    assert lb["n"] == "kestrel-public" and lb["scheme"] == "internet-facing"
    whens = {r["when"]: r["services"] for r in lb["routes"]}
    assert whens["path-pattern=/orders/*"] == ["prod-kestrel/shop-api"]
    assert whens["default"] == ["prod-kestrel/shop-api"]


def test_queues_data_and_named_resources() -> None:
    part = iac_terraform.read(TF, "infra/main.tf")
    q = {x["n"]: x for x in part["sqs"]}
    assert q["kestrel-orders.fifo"]["redrive"] is True and q["kestrel-orders-dlq.fifo"]["dlq"] is True
    assert part["rds"][0]["n"] == "kestrel-db" and part["rds"][0]["engine"] == "aurora-postgresql"
    assert part["lambda"][0]["mem"] == 1024
    assert part["sns"] == [{"n": "ops-alerts", "published": 0}] and part["s3"] == {"count": 1}


def test_braces_inside_strings_do_not_end_a_block() -> None:
    tf = 'resource "aws_sqs_queue" "q" {\n  name = "a}b"\n}\nresource "aws_sns_topic" "t" { name = "t1" }\n'
    part = iac_terraform.read(tf, "x.tf")
    assert [x["n"] for x in part["sqs"]] == ["a}b"] and part["sns"][0]["n"] == "t1"


def test_multiline_environment_objects_without_commas() -> None:
    tf = '''
resource "aws_ecs_task_definition" "t" {
  container_definitions = jsonencode([
    {
      name  = "api"
      image = "nginx"
      environment = [
        {
          name  = "ORDERS_QUEUE"
          value = "kestrel-orders.fifo"
        },
        {
          name  = "DB_HOST"
          value = "db"
        }
      ]
    }
  ])
}
resource "aws_ecs_service" "s" {
  name            = "api"
  task_definition = aws_ecs_task_definition.t.arn
}
'''
    (svc,) = iac_terraform.read(tf, "x.tf")["ecs"]
    assert {r["k"]: r["v"] for r in svc["refs"]} == {"ORDERS_QUEUE": "kestrel-orders.fifo", "DB_HOST": "db"}


def test_only_top_level_attributes_name_a_resource() -> None:
    tf = 'resource "aws_sns_topic" "t" {\n  tags {\n    name = "inner"\n  }\n  name = "outer"\n}\n'
    assert iac_terraform.read(tf, "x.tf")["sns"][0]["n"] == "outer"


def test_interpolation_with_quotes_comments_heredocs_and_count() -> None:
    tf = '''
/* resource "aws_sqs_queue" "commented" { name = "no" } */
resource "aws_sqs_queue" "a" { name = "${lookup(var.names, "env")}-orders" }
resource "aws_instance" "web" {
  user_data = <<-EOT
    resource "aws_sqs_queue" "inheredoc" {
      name = "no2"
    }
  EOT
}
resource "aws_sqs_queue" "off" {
  count = 0
  name  = "disabled"
}
resource "aws_lb_listener_rule" "r" { condition { path_pattern { values = ["/a/*", "/b/*"] } } }
'''
    assert [q["n"] for q in iac_terraform.read(tf, "x.tf")["sqs"]] == ["orders"]


def test_alb_names_and_forward_blocks() -> None:
    tf = '''
resource "aws_ecs_service" "s" {
  name = "api"
  load_balancer { target_group_arn = aws_alb_target_group.t.arn }
}
resource "aws_alb_target_group" "t" { name = "t" }
resource "aws_alb" "l" { name = "front" }
resource "aws_alb_listener" "https" {
  load_balancer_arn = aws_alb.l.arn
  port = 443
  default_action {
    type = "forward"
    forward {
      target_group { arn = aws_alb_target_group.t.arn }
    }
  }
}
'''
    (lb,) = iac_terraform.read(tf, "x.tf")["lb"]
    assert lb["routes"][0]["services"] == ["terraform/api"] and lb["routes"][0]["tg"] == "t"


def test_images_inside_json_heredoc_definitions() -> None:
    tf = '''
resource "aws_ecs_task_definition" "t" {
  container_definitions = <<EOF
[{"name": "web", "image": "nginx:1", "environment": [{"name": "ORDERS_QUEUE", "value": "${var.env}-orders"}]}]
EOF
}
resource "aws_ecs_service" "web" {
  name = "web"
  task_definition = aws_ecs_task_definition.t.arn
}
'''
    row = iac_terraform.read(tf, "main.tf")["ecs"][0]
    assert row["images"] == ["nginx:1"] and row["refs"] == [{"k": "ORDERS_QUEUE", "v": "orders"}]


def test_target_group_kind_and_attachments() -> None:
    tf = '''
resource "aws_lb" "pub" { name = "pub" }
resource "aws_lb_target_group" "fn" { target_type = "lambda" }
resource "aws_lb_target_group" "vm" { name = "vm" }
resource "aws_lb_target_group" "none" { name = "none" }
resource "aws_lb_target_group_attachment" "a" { target_group_arn = aws_lb_target_group.vm.arn }
resource "aws_lb_listener" "l" {
  load_balancer_arn = aws_lb.pub.arn
  default_action { target_group_arn = aws_lb_target_group.fn.arn }
}
resource "aws_lb_listener_rule" "r1" {
  listener_arn = aws_lb_listener.l.arn
  action { target_group_arn = aws_lb_target_group.vm.arn }
}
resource "aws_lb_listener_rule" "r2" {
  listener_arn = aws_lb_listener.l.arn
  action { target_group_arn = aws_lb_target_group.none.arn }
}
'''
    routes = {r["tg"]: r for r in iac_terraform.read(tf, "m.tf")["lb"][0]["routes"]}
    assert routes["fn"]["tgType"] == "lambda" and routes["vm"]["attached"] is True
    assert routes["none"]["attached"] is False and routes["none"]["tgType"] == "ip"


def test_hostile_engine_values_are_dropped() -> None:
    tf = '''
resource "aws_db_instance" "d" { engine = "<img src=x onerror=alert(1)>" }
resource "aws_elasticache_cluster" "c" { engine = "<svg onload=alert(1)>" }
resource "aws_db_instance" "ok" { engine = "postgres" }
'''
    part = iac_terraform.read(tf, "m.tf")
    assert {r["n"]: r["engine"] for r in part["rds"]} == {"d": "", "ok": "postgres"} and part["cache"][0]["engine"] == ""
