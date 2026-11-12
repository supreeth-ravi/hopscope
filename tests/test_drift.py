"""drift: code, infrastructure files and live AWS compared, with evidence and honest confidence."""
import json
from pathlib import Path

import drift
import iac_common as c

SQS = "https://sqs.us-east-1.amazonaws.com/" + "*" * 12 + "/"


def code_repo(base: Path) -> dict:
    files = {
        "api/app.py": 'ORDERS = "shop-orders.fifo"\nREFUNDS_QUEUE = "shop-refunds"\nGHOST = "shop-ghost.fifo"\n',
        "worker/main.py": "import boto3\nsqs.receive_message(QueueUrl=ORDERS)\n",
        "admin/server.js": "const db = 'x';\n",
    }
    for rel, text in files.items():
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text(text)
    return {"base": str(base), "services": ["api", "worker", "admin"], "files": [{"p": p, "x": []} for p in files], "routes": []}


def aws_row(cluster: str, n: str, desired: int = 2, images: tuple = (), refs: tuple = (), env: str = "prod", task: str = "") -> dict:
    return {"cluster": cluster, "n": n, "running": desired, "desired": desired, "images": list(images), "refs": [{"k": k, "v": v} for k, v in refs],
            "env": env, "taskDef": task, "ownImage": True, "tgs": []}


def live_inventory() -> dict:
    inv = c.empty_inventory("us-east-1")
    inv.update(mode="live")
    inv["ecs"] = [aws_row("prod-shop", "api", 3, ("shop-api",), (("Q", SQS + "shop-orders.fifo"), ("GONE", SQS + "shop-vanished")), task="shop-api:42"),
                  aws_row("staging-shop", "api", 1, ("shop-api",), env="staging"),
                  aws_row("prod-shop", "worker", 1, ("shop-worker",)),
                  aws_row("prod-shop", "recommendations", 1, ("reco",))]
    inv["sqs"] = [{"n": n, "dlq": "dlq" in n, "fam": f} for n, f in (("shop-orders.fifo", "shop-orders.fifo"), ("staging-shop-orders.fifo", "x"),
                                                                     ("shop-orders-dlq.fifo", "y"), ("promo-01", "promo-*"), ("promo-02", "promo-*"))]
    inv["rds"] = [{"n": "shop-db", "endpoint": "shop-db-1.xxxxxxxx.us-east-1.rds.amazonaws.com", "clusterEndpoint": ""},
                  {"n": "analytics-old", "endpoint": "", "clusterEndpoint": ""}]
    return inv


def files_inventory() -> dict:
    inv = c.empty_inventory("design")
    inv["ecs"] = [c.ecs_row("shop", "api", desired=2, images=["${aws_ecr_repository.api.repository_url}:latest"], source="terraform", desired_set=True,
                            family="shop-api", build_from="infra/main.tf"),
                  c.ecs_row("shop", "worker", desired=1, images=["registry.example/other-worker:1"], source="terraform",
                            desired_set=False, build_from="infra/main.tf"),
                  c.ecs_row("local", "admin", build="admin", source="compose", build_from="docker-compose.yml")]
    inv["sqs"] = [c.sqs_row("shop-orders.fifo"), c.sqs_row("shop-wholesale.fifo")]
    inv["rds"] = [c.rds_row("shop-db", "aurora-postgresql")]
    inv["sources"] = ["infra/main.tf", "docker-compose.yml"]
    return inv


def links() -> dict:
    return {"placement": {"prod-shop/api": {"code": "api", "confidence": 0.9}, "prod-shop/worker": {"code": "worker", "confidence": 0.7}},
            "queues": {"api": ["shop-orders.fifo"]}}


def by_kind(rep: dict) -> dict:
    out: dict = {}
    for f in rep["findings"]:
        out.setdefault(f["kind"], []).append(f)
    return out


def test_name_matching_is_tolerant_and_honest() -> None:
    assert drift.match("prod-shop-api", ["shop-api"]) == ("shop-api", "env")
    assert drift.match("shop-orders.fifo", ["shop-orders.fifo"]) == ("shop-orders.fifo", "exact")
    assert drift.match("kestrel-cache-001", ["kestrel-cache"]) == ("kestrel-cache", "env")
    assert drift.match("shop-orders.fifo", ["orders.fifo"]) == ("orders.fifo", "prefix")  # an interpolated prefix was dropped from the file name
    assert drift.match("orders", ["old-orders", "new-orders"])[1] == "low"  # two equally good candidates: not sure
    assert drift.match("billing", ["shop-api"]) == (None, None)


def test_three_way_findings(tmp_path: Path) -> None:
    code = code_repo(tmp_path / "repo")
    rep = drift.report(code, links(), live_inventory(), files_inventory())
    k = by_kind(rep)
    assert rep["mode"] == "live" and rep["sources"]["aws"] is True and rep["sources"]["files"] == ["infra/main.tf", "docker-compose.yml"]
    not_in_files = {f["resource"]: f for f in k["aws_not_in_files"]}
    assert "prod-shop/recommendations" in not_in_files and "analytics-old" in not_in_files
    assert not_in_files["promo-*"]["count"] == 2  # a family is reported once, with its size
    assert not any("api" in f["names"] for f in k["aws_not_in_files"])  # env prefixes are stripped: prod and staging api match
    assert {f["resource"] for f in k["files_not_deployed"]} == {"shop-wholesale.fifo"}
    d = k["desired_differs"][0]
    assert d["resource"] == "prod-shop/api" and "2" in d["title"] and "3" in d["title"] and d["evidence"]
    assert len(k["desired_differs"]) == 1  # the worker's file sets no literal count
    assert {f["resource"] for f in k["image_differs"]} == {"prod-shop/worker"}  # a variable image is not compared
    assert k["ref_missing"][0]["resource"] == "prod-shop/api" and "shop-vanished" in k["ref_missing"][0]["title"]
    assert {f["resource"] for f in k["queue_missing"]} == {"shop-ghost.fifo"}  # REFUNDS_QUEUE = "..." may be another broker's queue
    assert [f["resource"] for f in k["queue_unreferenced"]] == ["promo-*"]
    assert all(f["confidence"] in ("high", "low") and f["evidence"] for f in rep["findings"])
    assert rep["uncertain"] == []  # similar-name notes are off by default


def test_order_puts_differences_before_not_deployed_before_not_in_files(tmp_path: Path) -> None:
    rep = drift.report(code_repo(tmp_path / "repo"), links(), live_inventory(), files_inventory())
    kinds = [f["kind"] for f in rep["findings"]]
    assert kinds.index("image_differs") < kinds.index("files_not_deployed") < kinds.index("aws_not_in_files")
    assert "in AWS but in no file: 1 queue family (2 queues), 1 ECS service, 1 queue, 1 database" in rep["summary"]


def test_a_dropped_prefix_is_not_a_match(tmp_path: Path) -> None:
    files = files_inventory()
    files["sqs"] = [c.sqs_row("orders.fifo")]  # ${var.prefix}-orders read as just "orders"
    rep = drift.report(code_repo(tmp_path / "repo"), links(), live_inventory(), files)
    k = by_kind(rep)
    assert "orders.fifo" in {f["resource"] for f in k["files_not_deployed"]}
    rep = drift.report(code_repo(tmp_path / "repo2"), links(), live_inventory(), files, similar=True)
    assert any(u["files"] == "orders.fifo" for u in rep["uncertain"])


def service_repo(base: Path) -> dict:
    code = code_repo(base)
    code["services"] += ["backend", "payments", "email", "admin-ui"]
    return code


def test_shared_words_never_make_a_build_or_image_finding(tmp_path: Path) -> None:
    live = live_inventory()
    live["ecs"] = [aws_row("prod-x", "staff-backend", 1, ("staff-backend",)), aws_row("prod-x", "billing-charge-workers", 1, ("payments",))]
    files = files_inventory()
    files["ecs"] = [c.ecs_row("app", "backend", build="backend", images=["backend"], source="compose", build_from="docker-compose.yml"),
                    c.ecs_row("app", "mailer-worker", build="email", images=["email"], source="compose", build_from="docker-compose.yml")]
    lk = {"placement": {"prod-x/staff-backend": {"code": "admin", "confidence": 0.7}, "prod-x/billing-charge-workers": {"code": "payments", "confidence": 0.8}}}
    rep = drift.report(service_repo(tmp_path / "repo"), lk, live, files)
    bad = [f for f in rep["findings"] if f["kind"] in ("link_may_be_wrong", "image_differs", "desired_differs", "taskdef_differs")]
    assert bad == []


def test_same_code_folder_is_a_match(tmp_path: Path) -> None:
    live = live_inventory()
    live["ecs"] = [aws_row("prod-x", "storefront-svc", 1, ("shop-web",))]
    files = files_inventory()
    files["ecs"].append(c.ecs_row("app", "web", build="worker", images=["shop-web-local"], source="compose", build_from="docker-compose.yml"))
    lk = {"placement": {"prod-x/storefront-svc": {"code": "worker", "confidence": 0.9}}}
    rep = drift.report(code_repo(tmp_path / "repo"), lk, live, files)
    k = by_kind(rep)
    assert "prod-x/storefront-svc" not in {f["resource"] for f in k.get("aws_not_in_files", [])}
    assert {f["resource"] for f in k["image_differs"]} == {"prod-x/storefront-svc"}
    assert "same code folder" in k["image_differs"][0]["evidence"][0]


def test_link_may_be_wrong(tmp_path: Path) -> None:
    live = live_inventory()
    live["ecs"].append(aws_row("prod-shop", "admin", 1, ("admin",)))
    lk = links()
    lk["placement"]["prod-shop/admin"] = {"code": "api", "confidence": 0.6, "evidence": ["name match: shop"]}
    rep = drift.report(code_repo(tmp_path / "repo"), lk, live, files_inventory())
    f = by_kind(rep)["link_may_be_wrong"][0]
    assert f["resource"] == "prod-shop/admin" and "may be wrong" in f["title"] and "admin" in f["title"] and "api" in f["title"]
    assert rep["findings"][0]["kind"] == "link_may_be_wrong"


def test_queue_families_are_grouped_and_a_static_prefix_counts_as_a_reference(tmp_path: Path) -> None:
    code = code_repo(tmp_path / "repo")
    (tmp_path / "repo" / "worker" / "tenants.py").write_text('def q(t):\n    return f"tenant_events_{t}.fifo"\n')
    code["files"].append({"p": "worker/tenants.py", "x": []})
    live = live_inventory()
    live["sqs"] = ([{"n": f"tenant_events_{i:08x}.fifo", "fam": "tenant_events_*.fifo"} for i in range(30)]
                   + [{"n": f"tenant_sync_{i:08x}", "fam": "tenant_sync_*"} for i in range(5)]
                   + [{"n": "old-a", "fam": "old-a"}, {"n": "old-b", "fam": "old-b"}, {"n": "shop-orders.fifo", "fam": "shop-orders.fifo"}])
    rep = drift.report(code, links(), live, files_inventory())
    unref = {f["resource"]: f for f in by_kind(rep)["queue_unreferenced"]}
    assert set(unref) == {"tenant_sync_*", "2 queues"}
    assert unref["tenant_sync_*"]["count"] == 5 and len(unref["tenant_sync_*"]["names"]) == 5
    assert unref["2 queues"]["names"] == ["old-a", "old-b"]
    nif = {f["resource"]: f for f in by_kind(rep)["aws_not_in_files"] if f["resourceKind"] == "sqs"}
    assert nif["tenant_events_*.fifo"]["count"] == 30 and len(nif["tenant_events_*.fifo"]["names"]) == 5
    assert len(rep["findings"]) < 15


def test_design_mode_runs_code_and_files_checks_only(tmp_path: Path) -> None:
    code = code_repo(tmp_path / "repo")
    files = files_inventory()
    files["ecs"][0]["refs"] = [{"k": "AUDIT_QUEUE", "v": "shop-audit"}]
    rep = drift.report(code, {"placement": {}, "queues": {}}, None, files)
    k = by_kind(rep)
    assert rep["mode"] == "design" and rep["sources"]["aws"] is False
    assert not k.get("aws_not_in_files") and not k.get("files_not_deployed") and not k.get("desired_differs")
    assert [f["resource"] for f in k["queue_unreferenced"]] == ["shop-wholesale.fifo"]
    assert {f["resource"] for f in k["queue_missing"]} == {"shop-ghost.fifo"}
    assert k["ref_missing"][0]["title"].startswith("shop/api: AUDIT_QUEUE")
    assert any("AWS" in s for s in rep["skipped"])


def test_design_mode_link_check(tmp_path: Path) -> None:
    files = files_inventory()
    rep = drift.report(code_repo(tmp_path / "repo"), {"placement": {"local/admin": {"code": "api", "confidence": 1.0}}}, None, files)
    assert by_kind(rep)["link_may_be_wrong"][0]["resource"] == "local/admin"


def test_live_without_files_skips_file_checks(tmp_path: Path) -> None:
    rep = drift.report(code_repo(tmp_path / "repo"), links(), live_inventory(), None)
    assert not by_kind(rep).get("aws_not_in_files") and any("infrastructure files" in s for s in rep["skipped"])
    assert by_kind(rep)["queue_unreferenced"]


def test_actions_are_few_counted_and_grouped(tmp_path: Path) -> None:
    rep = drift.report(code_repo(tmp_path / "repo"), links(), live_inventory(), files_inventory())
    acts = drift.actions(rep)
    assert 1 <= len(acts) <= 3 and all(len(a) == 4 and a[0] == "warn" for a in acts)
    assert acts[0][1].startswith("3 difference")
    assert any(a[1] == "In AWS but in no infrastructure file: 1 queue family (2 queues), 1 ECS service, 1 queue, 1 database" for a in acts)


def test_cli_writes_drift_json(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "code.json").write_text(json.dumps(code_repo(tmp_path / "repo")))
    (data / "links.json").write_text(json.dumps(links()))
    (data / "aws.json").write_text(json.dumps(live_inventory()))
    (data / "iac.json").write_text(json.dumps(files_inventory()))
    drift.main(["--data", str(data)])
    out = json.loads((data / "drift.json").read_text())
    assert out["findings"] and out["mode"] == "live" and out["uncertain"] == []


def test_queue_names_need_sqs_and_application_code(tmp_path: Path) -> None:
    code = code_repo(tmp_path / "repo")
    (tmp_path / "repo" / "api" / "tests").mkdir()
    (tmp_path / "repo" / "api" / "tests" / "test_q.py").write_text('Q = "fixture-only.fifo"\n')
    (tmp_path / "repo" / "api" / "celery.py").write_text('MAIN_QUEUE = "jobs"\n')
    code["files"] += [{"p": "api/tests/test_q.py", "x": []}, {"p": "api/celery.py", "x": []}]
    live = live_inventory()
    rep = drift.report(code, links(), live, files_inventory())
    missing = {f["resource"] for f in rep["findings"] if f["kind"] == "queue_missing"}
    assert "fixture-only.fifo" not in missing and "jobs" not in missing
    live["sqs"] = []
    files = files_inventory()
    files["sqs"] = []
    rep = drift.report(code, links(), live, files)
    assert not [f for f in rep["findings"] if f["kind"] in ("queue_missing", "queue_unreferenced")]


def test_a_variable_name_prefix_is_a_match_but_a_plain_one_is_not(tmp_path: Path) -> None:
    live = live_inventory()
    live["ecs"] = [aws_row("prod-x", "email-worker", 4, ("email-worker",))]
    files = files_inventory()
    files["ecs"] = [c.ecs_row("x", "worker", desired=1, images=["other"], source="terraform", desired_set=True, build_from="infra/main.tf")]
    rep = drift.report(code_repo(tmp_path / "repo"), {"placement": {}}, live, files)
    k = by_kind(rep)
    assert not k.get("desired_differs") and not k.get("image_differs")
    assert "prod-x/email-worker" in {f["resource"] for f in k["aws_not_in_files"]}
    files["ecs"][0]["nameVar"] = True  # name = "${var.team}-worker" in the file
    k = by_kind(drift.report(code_repo(tmp_path / "repo2"), {"placement": {}}, live, files))
    assert k["desired_differs"][0]["resource"] == "prod-x/email-worker"


def test_same_named_rows_prefer_the_same_environment(tmp_path: Path) -> None:
    live = live_inventory()
    live["ecs"] = [aws_row("staging-shop", "api", 1, ("shop-api",), env="staging")]
    files = files_inventory()
    files["ecs"] = [c.ecs_row("prod", "api", desired=5, source="terraform", desired_set=True, build_from="a.tf"),
                    c.ecs_row("staging", "api", desired=1, source="terraform", desired_set=True, build_from="b.tf")]
    rep = drift.report(code_repo(tmp_path / "repo"), {"placement": {}}, live, files)
    assert not by_kind(rep).get("desired_differs")


def test_design_mode_missing_reference_may_live_in_another_repo(tmp_path: Path) -> None:
    files = files_inventory()
    files["ecs"][0]["refs"] = [{"k": "REFUNDS_QUEUE", "v": "shop-refunds"}, {"k": "AUDIT_QUEUE", "v": "shop-audit"}]
    rep = drift.report(code_repo(tmp_path / "repo"), {"placement": {}, "queues": {}}, None, files)
    elsewhere = "It isn't defined in these files; it may be defined in another repo or created outside infrastructure files."
    by_q = {("shop-refunds" if "shop-refunds" in f["title"] else "shop-audit"): f for f in by_kind(rep)["ref_missing"]}
    assert all(f["detail"] == elsewhere for f in by_q.values())
    assert "does not exist" not in by_q["shop-refunds"]["title"] and "not defined in these files" in by_q["shop-refunds"]["title"]
    # the code names shop-refunds (api/app.py): probably created elsewhere, so informational only
    assert by_q["shop-refunds"]["severity"] == "info" and by_q["shop-refunds"]["confidence"] == "low"
    assert any("api/app.py" in e for e in by_q["shop-refunds"]["evidence"])
    # shop-audit is in no scanned repo at all: still a warning
    assert by_q["shop-audit"]["severity"] == "warn"
    broken = [a for a in drift.actions(rep) if "reference(s)" in a[1]]
    assert broken and broken[0][1].startswith("1 reference(s)") and "shop-audit" in broken[0][2] and "shop-refunds" not in broken[0][2]


def test_design_mode_reference_named_in_code_is_not_a_warning_action(tmp_path: Path) -> None:
    files = files_inventory()
    files["ecs"][0]["refs"] = [{"k": "REFUNDS_QUEUE", "v": "shop-refunds"}]
    rep = drift.report(code_repo(tmp_path / "repo"), {"placement": {}, "queues": {}}, None, files)
    assert [f["severity"] for f in by_kind(rep)["ref_missing"]] == ["info"]
    assert not any("shop-refunds" in " ".join(a) for a in drift.actions(rep))
    assert "broken settings" not in rep["summary"]


def test_live_mode_missing_reference_wording_is_unchanged(tmp_path: Path) -> None:
    rep = drift.report(code_repo(tmp_path / "repo"), links(), live_inventory(), files_inventory())
    f = by_kind(rep)["ref_missing"][0]
    assert f["title"] == "prod-shop/api: GONE points at queue shop-vanished, which does not exist in AWS"
    assert f["detail"] == "The service is configured for a resource that isn't there: calls to it fail." and f["severity"] == "warn"


def test_code_texts_stay_under_the_base_and_skip_symlinks(tmp_path: Path) -> None:
    import drift_code
    base = tmp_path / "repo"
    (base / "api").mkdir(parents=True)
    (base / "api" / "app.py").write_text('Q = "inside.fifo"\n')
    (tmp_path / "secret.py").write_text('Q = "outside.fifo"\n')
    (base / "api" / "link.py").symlink_to(tmp_path / "secret.py")
    (base / "api" / "inner.py").symlink_to(base / "api" / "app.py")
    code = {"base": str(base), "files": [{"p": "api/app.py"}, {"p": "../secret.py"}, {"p": "api/../../secret.py"}, {"p": str(tmp_path / "secret.py")},
                                         {"p": "api/link.py"}, {"p": "api/inner.py"}]}
    got = drift_code.texts(code)
    assert [p for p, _ in got] == ["api/app.py"]
    assert all("outside" not in t for _, t in got)


def test_console_drift_badges_stay_on_one_line() -> None:
    html = (Path(__file__).resolve().parents[1] / "skills" / "hopscope" / "assets" / "console.html").read_text()
    assert ".drift summary .sev, .drift summary .pill { white-space: nowrap; flex-shrink: 0 }" in html
    assert '<span class="dt">${esc(x.title)}</span>' in html and ".drift summary .dt { flex: 1 1 auto; min-width: 0" in html


def test_design_mode_missing_queue_may_be_defined_elsewhere(tmp_path: Path) -> None:
    rep = drift.report(code_repo(tmp_path / "repo"), {"placement": {}, "queues": {}}, None, files_inventory())
    f = by_kind(rep)["queue_missing"][0]
    assert f["title"] == "Code references queue shop-ghost.fifo, which is not defined in these files"
    assert f["detail"] == "It isn't defined in these files; it may be defined in another repo or created at runtime."
    assert f["severity"] == "info" and f["confidence"] == "low"
    assert not any("shop-ghost" in " ".join(a) for a in drift.actions(rep))
    assert "missing queues" not in rep["summary"]


def test_live_mode_missing_queue_wording_is_unchanged(tmp_path: Path) -> None:
    rep = drift.report(code_repo(tmp_path / "repo"), links(), live_inventory(), files_inventory())
    f = by_kind(rep)["queue_missing"][0]
    assert f["title"] == "Code references queue shop-ghost.fifo, which is not in AWS"
    assert f["detail"] == "Messages sent to it fail, or it lives in another account or region." and f["severity"] == "warn"
    assert any("shop-ghost" in " ".join(a) for a in drift.actions(rep))
