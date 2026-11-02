from pathlib import Path

import linker


def inv() -> dict:
    def ecs(c: str, n: str, images: tuple = (), refs: tuple = ()) -> dict:
        return {"cluster": c, "n": n, "images": list(images), "containers": [n], "refs": list(refs), "env": "prod", "running": 1}
    return {"ecs": [ecs("prod", "platform-backend", ["acme/backend"]), ecs("prod", "admin-backend", ["acme/admin"]),
                    ecs("prod", "usage-consumer", refs=[{"k": "QUEUE_URL", "v": "https://sqs.ap-south-1.amazonaws.com/1/usage-events.fifo"}])],
            "sqs": [{"n": "usage-events.fifo", "sent": 10, "backlog": 0}], "ec2": [], "lb": [], "rds": []}


def code(tmp: Path) -> dict:
    files = []
    for svc in ("backend", "admin", "usage"):
        (tmp / svc).mkdir()
        (tmp / svc / "main.py").write_text("import os\nQ = 'usage-events'\n" if svc != "admin" else "x = 1\n")
        files.append({"p": f"{svc}/main.py", "x": ["postgres"] if svc == "usage" else []})
    return {"base": str(tmp), "services": ["backend", "admin", "usage"], "files": files}


def test_rare_word_beats_common_word(tmp_path: Path) -> None:
    placement = linker.score_placement(code(tmp_path), inv(), [], ["backend", "admin", "usage"])
    assert placement["prod/admin-backend"]["code"] == "admin"
    assert placement["prod/platform-backend"]["code"] == "backend"


def test_deploy_file_evidence_raises_confidence(tmp_path: Path) -> None:
    deploys = [("usage/Dockerfile", "# image for usage-consumer\n")]
    placement = linker.score_placement(code(tmp_path), inv(), deploys, ["backend", "admin", "usage"])
    assert placement["prod/usage-consumer"]["code"] == "usage"
    assert placement["prod/usage-consumer"]["confidence"] >= 0.75


def test_queue_found_in_code_and_env(tmp_path: Path) -> None:
    c = code(tmp_path)
    placement = linker.score_placement(c, inv(), [("usage/Dockerfile", "usage-consumer")], c["services"])
    queues, workers = linker.queue_links(c, inv(), c["services"], placement, tmp_path)
    assert "usage-events.fifo" in queues["usage"]
    assert "usage-events.fifo" in queues["backend"]
    assert workers["usage-events.fifo"][0] == "usage-consumer"
