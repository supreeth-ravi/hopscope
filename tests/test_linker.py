from pathlib import Path

import linker


def inv() -> dict:
    def ecs(c: str, n: str, images: tuple = (), refs: tuple = ()) -> dict:
        return {"cluster": c, "n": n, "images": list(images), "containers": [n], "refs": list(refs), "env": "prod", "running": 1}
    return {"ecs": [ecs("prod", "shop-backend", ["kestrel/backend"]), ecs("prod", "staff-backend", ["kestrel/staff"]),
                    ecs("prod", "orders-consumer", refs=[{"k": "QUEUE_URL", "v": "https://sqs.ap-south-1.amazonaws.com/1/order-events.fifo"}])],
            "sqs": [{"n": "order-events.fifo", "sent": 10, "backlog": 0}], "ec2": [], "lb": [], "rds": []}


def code(tmp: Path) -> dict:
    files = []
    for svc in ("backend", "staff", "orders"):
        (tmp / svc).mkdir()
        (tmp / svc / "main.py").write_text("import os\nQ = 'order-events'\n" if svc != "staff" else "x = 1\n")
        files.append({"p": f"{svc}/main.py", "x": ["postgres"] if svc == "orders" else []})
    return {"base": str(tmp), "services": ["backend", "staff", "orders"], "files": files}


def test_rare_word_beats_common_word(tmp_path: Path) -> None:
    placement = linker.score_placement(code(tmp_path), inv(), [], ["backend", "staff", "orders"])
    assert placement["prod/staff-backend"]["code"] == "staff"
    assert placement["prod/shop-backend"]["code"] == "backend"


def test_deploy_file_evidence_raises_confidence(tmp_path: Path) -> None:
    deploys = [("orders/Dockerfile", "# image for orders-consumer\n")]
    placement = linker.score_placement(code(tmp_path), inv(), deploys, ["backend", "staff", "orders"])
    assert placement["prod/orders-consumer"]["code"] == "orders"
    assert placement["prod/orders-consumer"]["confidence"] >= 0.75


def test_queue_found_in_code_and_env(tmp_path: Path) -> None:
    c = code(tmp_path)
    placement = linker.score_placement(c, inv(), [("orders/Dockerfile", "orders-consumer")], c["services"])
    queues, workers = linker.queue_links(c, inv(), c["services"], placement, tmp_path)
    assert "order-events.fifo" in queues["orders"]
    assert "order-events.fifo" in queues["backend"]
    assert workers["order-events.fifo"][0] == "orders-consumer"


def test_build_folder_places_exactly(tmp_path: Path) -> None:
    i = inv()
    i["ecs"].append({"cluster": "compose", "n": "zzz", "images": [], "containers": ["zzz"], "refs": [], "env": "prod", "running": 1,
                     "build": "staff/docker", "buildFrom": "docker-compose.yml"})
    placement = linker.score_placement(code(tmp_path), i, [], ["backend", "staff", "orders"])
    assert placement["compose/zzz"] == {"code": "staff", "confidence": 1.0, "evidence": ["docker-compose.yml builds staff/docker"]}


def test_build_evidence_with_a_file_list(tmp_path: Path) -> None:
    i = inv()
    i["ecs"].append({"cluster": "tf", "n": "zzz", "images": [], "containers": ["zzz"], "refs": [], "env": "prod", "running": 1,
                     "build": "orders", "buildFrom": "infra/a.tf, infra/b.tf, infra/c.tf"})
    placement = linker.score_placement(code(tmp_path), i, [], ["backend", "staff", "orders"])
    assert placement["tf/zzz"]["evidence"] == ["infra/a.tf (+2 more) builds orders"]


def test_design_mode_links_queues_with_no_traffic(tmp_path: Path) -> None:
    i = inv()
    i["mode"] = "design"
    i["sqs"] = [{"n": "order-events.fifo", "sent": 0, "backlog": 0}]
    c = code(tmp_path)
    placement = linker.score_placement(c, i, [], c["services"])
    queues, _ = linker.queue_links(c, i, c["services"], placement, tmp_path)
    assert "order-events.fifo" in queues["orders"]


def design_queue_links(tmp_path: Path, files: dict[str, str], queue: str, refs: list | None = None) -> dict:
    services = []
    entries = []
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
        entries.append({"p": rel, "x": []})
        services.append(rel.split("/")[0])
    c = {"base": str(tmp_path), "services": services, "files": entries}
    i = {"mode": "design", "ecs": [{"cluster": "k", "n": "svc", "images": [], "containers": [], "refs": refs or [], "env": "prod", "running": 1}],
         "sqs": [{"n": queue, "sent": 0, "backlog": 0}], "ec2": [], "lb": [], "rds": []}
    placement = {"k/svc": {"code": services[0], "confidence": 1.0, "evidence": []}} if refs else {}
    return linker.queue_links(c, i, services, placement, tmp_path)[0]


def test_design_queue_needs_a_real_reference(tmp_path: Path) -> None:
    text = "orders_total = 3\nprint('Total orders processed')\n# orders are great\n"
    assert design_queue_links(tmp_path, {"a/x.py": text}, "orders") == {}


def test_design_queue_short_literal_needs_queue_context(tmp_path: Path) -> None:
    assert design_queue_links(tmp_path, {"a/x.py": "kind = 'jobs'\n"}, "jobs") == {}
    assert design_queue_links(tmp_path, {"a/x.py": "sqs.send_message(QueueUrl='jobs')\n"}, "jobs") == {"a": ["jobs"]}
    assert design_queue_links(tmp_path, {"a/x.py": "name = 'kestrel-emails'\n"}, "kestrel-emails") == {"a": ["kestrel-emails"]}


def test_design_queue_from_env_var_name_and_url(tmp_path: Path) -> None:
    assert design_queue_links(tmp_path, {"a/x.py": 'q = os.environ["ORDERS_QUEUE"]\n'}, "orders") == {"a": ["orders"]}
    assert design_queue_links(tmp_path, {"a/x.py": 'u = "https://sqs.eu-west-1.amazonaws.com/123456789012/orders"\n'}, "orders") == {"a": ["orders"]}
    assert design_queue_links(tmp_path, {"a/x.py": '@app.get("/orders")\n'}, "orders") == {}


def test_design_queue_from_container_env(tmp_path: Path) -> None:
    out = design_queue_links(tmp_path, {"a/x.py": "x = 1\n"}, "orders", refs=[{"k": "ORDERS_QUEUE", "v": "orders"}])
    assert out == {"a": ["orders"]}
