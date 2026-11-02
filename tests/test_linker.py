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
