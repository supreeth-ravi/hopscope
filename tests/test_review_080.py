"""Fixes from the 0.8.0 review: overrides on a merged inventory, history across regions, masked warnings, linear model
scans, Rails concerns and shallow routes, and smaller hardening. Invented fixtures only."""
from __future__ import annotations

import json
from pathlib import Path

import linker


def merged_inv() -> dict:
    def ecs(cluster: str, region: str) -> dict:
        return {"cluster": cluster, "n": "shop-api", "images": ["shop-api"], "containers": ["api"], "refs": [], "env": "prod", "running": 1,
                "region": region, "account": "kestrel-prod"}
    return {"ecs": [ecs("eu-west-1:kestrel", "eu-west-1"), ecs("us-east-1:kestrel", "us-east-1")], "sqs": [
        {"n": f"{r}:orders", "sent": 1, "backlog": 0, "region": r} for r in ("eu-west-1", "us-east-1")], "ec2": [], "lb": [], "rds": []}


def test_plain_override_keys_expand_to_every_prefixed_service(capsys) -> None:
    inv = merged_inv()
    keys = [f'{e["cluster"]}/{e["n"]}' for e in inv["ecs"]]
    ov = {"placement": {"kestrel/shop-api": "shop", "eu-west-1:kestrel/shop-api": "shop", "nowhere/ghost": "shop"}}
    got = linker.apply_overrides({}, ov, ["shop"], keys)
    assert sorted(got["placement"]) == sorted(keys)
    assert "nowhere/ghost" not in got["placement"] and "nowhere/ghost" in capsys.readouterr().err
    got = linker.apply_overrides({k: {"code": "shop", "confidence": 0.9, "evidence": []} for k in keys}, {"placement": {"kestrel/shop-api": None}}, ["shop"], keys)
    assert got["placement"] == {} and got["ignored"] == set(keys)


def test_queue_links_tolerate_a_placement_with_no_service(tmp_path: Path) -> None:
    code = {"base": str(tmp_path), "services": ["shop"], "files": []}
    placement = {"kestrel/shop-api": {"code": "shop", "confidence": 1.0}}  # a plain key the merged inventory doesn't have
    linker.queue_links(code, merged_inv(), ["shop"], placement, tmp_path)
    assert linker.db_links(merged_inv(), placement) == {}


def test_queue_worker_overrides_expand_to_prefixed_queues() -> None:
    assert sorted(linker.expand_workers({"orders": ["orders-worker", "process message"]}, merged_inv())) == ["eu-west-1:orders", "us-east-1:orders"]
    assert json.dumps(linker.expand_workers({"exact": ["w", "j"]}, merged_inv())) == '{"exact": ["w", "j"]}'


def test_a_setting_naming_no_known_queue_adds_no_empty_entry(tmp_path: Path) -> None:
    inv = merged_inv()
    inv["ecs"][0]["refs"] = [{"k": "QUEUE_URL", "v": "https://sqs.eu-west-1.amazonaws.com/" + "*" * 12 + "/unknown"}]
    code = {"base": str(tmp_path), "services": ["shop"], "files": []}
    queues, _ = linker.queue_links(code, inv, ["shop"], {"eu-west-1:kestrel/shop-api": {"code": "shop", "confidence": 1.0}}, tmp_path)
    assert queues == {}


def test_history_skipped_region_removes_only_its_own_rows() -> None:
    import aws_merge
    import history
    from test_multi_merge import GLOBALS, inv, ts

    both = [{"account": "kestrel-prod", "region": r, "inv": inv(r), "ts": ts(r, [1.0])} for r in ("eu-west-1", "us-east-1")]
    run1, _ = aws_merge.merge(both, GLOBALS, [])
    run2, _ = aws_merge.merge(both[:1], GLOBALS, [{"account": "kestrel-prod", "region": "us-east-1", "why": "ExpiredToken"}])
    d = history.diff_resources(history.resources(run1), history.resources(run2))
    assert d["added"] == [] and d["changed"] == []
    assert d["removed"] and all(r["name"].startswith("us-east-1:") or r["name"] == "orders" or "us-east-1" in r["name"] for r in d["removed"])
    names = {r["name"] for r in d["removed"]}
    assert "us-east-1:kestrel/shop-api" in names and "us-east-1:kestrel-public" in names and "us-east-1:kestrel-orders" in names


def test_single_scope_history_keys_are_unchanged() -> None:
    import history

    assert set(history.resources({"sqs": [{"n": "orders", "dlq": False}], "dynamodb": ["t"]})) == {"sqs:orders", "dynamodb:t"}


def test_collector_warnings_mask_account_numbers(capsys) -> None:
    import aws_inventory

    acct = "7" * 12

    def denied():
        raise PermissionError(f"User: arn:aws:sts::{acct}:assumed-role/ops/kestrel is not authorized to perform: sqs:ListQueues")

    assert aws_inventory.safe("sqs", denied) is None
    out = capsys.readouterr().out
    assert acct not in out and "*" * 12 in out


def test_model_use_checks_are_one_pass_per_file() -> None:
    import time

    from langs import php, ruby

    models = {f"Bean{i}" for i in range(600)}
    rb = "class RoastsController\n  def show\n    @x = Roast.where(a: 1)\n  end\nend\n" * 5
    ph = "<?php\nclass RoastController {\n  public function show() { return Roast::query(); }\n}\n" * 5
    t0 = time.perf_counter()
    for _ in range(1000):
        assert not ruby._uses_ar(rb, models)
        assert not php._uses_models(ph, models)
    assert time.perf_counter() - t0 < 3
    assert ruby._uses_ar(rb, models | {"Roast"}) and php._uses_models(ph, models | {"Roast"})
    assert not ruby._uses_ar("Admin::Roast.where(x)", {"Roast"}) and ruby._uses_ar("Admin::Roast.where(x)", {"Admin::Roast"})


ROUTES_RB = '''Rails.application.routes.draw do
  concern :commentable do
    resources :comments, only: [:index, :create]
  end
  resources :articles, concerns: :commentable, shallow: true do
    resources :tags
  end
  resources :photos, shallow: true do
    resources :comments
  end
  resources :teas, only: [] do
    member { post :steep }
    collection {
      get :search
    }
    concerns :commentable
  end
end
'''


def test_rails_concerns_shallow_and_brace_member_routes() -> None:
    from langs import rails

    got = {(r.verb, r.path, r.controller, r.action) for r in rails.parse(ROUTES_RB)}
    paths = {(v, p) for v, p, _, _ in got}
    assert ("GET", "/comments") not in paths and ("POST", "/comments") not in paths  # a concern body is not a route by itself
    assert {("GET", "/articles/:article_id/comments"), ("POST", "/articles/:article_id/comments")} <= paths
    assert {("GET", "/articles/:article_id/tags"), ("POST", "/articles/:article_id/tags"), ("GET", "/articles/:article_id/tags/new"),
            ("GET", "/tags/:id"), ("GET", "/tags/:id/edit"), ("PATCH", "/tags/:id"), ("DELETE", "/tags/:id")} <= paths
    assert ("GET", "/articles/:article_id/tags/:id") not in paths
    assert {("GET", "/photos/:id"), ("GET", "/photos/:photo_id/comments"), ("GET", "/photos/:photo_id/comments/new"),
            ("GET", "/comments/:id"), ("DELETE", "/comments/:id")} <= paths
    assert ("GET", "/photos/:photo_id/comments/:id") not in paths
    assert ("POST", "/teas/:id/steep", "teas", "steep") in got and ("GET", "/teas/search", "teas", "search") in got
    assert ("GET", "/teas/:tea_id/comments") in paths


def test_ruby_word_lists_are_not_keywords() -> None:
    from langs.rubytext import blank, blocks

    src = "def steep\n  kinds = %w[if do end while]\n  puts %w[do end]\n  log %i|case end|\n  marks = %i(begin case)\n  more = %W{unless until}\n  x = %I<do end>\n  1\nend\n"
    got = [(b.kw, src[b.end:b.end + 3]) for b in blocks(blank(src, True))]
    assert got == [("def", "end")]


def test_account_flags_reject_trailing_newlines() -> None:
    import pytest

    import aws_accounts

    with pytest.raises(SystemExit):
        aws_accounts.regions(None, ["eu-west-1\n"], False)
    assert aws_accounts.REGION.fullmatch("eu-west-1") and not aws_accounts.REGION.fullmatch("eu-west-1\n")
    assert not aws_accounts.ACCOUNT_ID.fullmatch("1" * 12 + "\n") and not aws_accounts.ACCOUNT_ID.fullmatch("١" * 12)
    assert not aws_accounts.ROLE_NAME.fullmatch("Hopscope\n") and not aws_accounts.ALIAS.fullmatch("kestrel\n")


def test_assumed_role_session_is_limited_to_the_read_only_policy() -> None:
    import aws_accounts
    from test_multi_accounts import IAM, STS, Orgs, Session

    acct = "1" * 8 + "4321"
    sts = STS("9" * 12)
    base = Session(sts=sts, organizations=Orgs([(acct, "ACTIVE")]))
    aws_accounts.from_org(base, "HopscopeReadOnly", [], lambda c, r: Session(iam=IAM([])))
    policy = json.loads(sts.assumed[0]["Policy"])
    actions = policy["Statement"][0]["Action"]
    assert policy["Statement"][0]["Effect"] == "Allow" and "sqs:ListQueues" in actions and "cloudwatch:GetMetricData" in actions
    assert not any(a.split(":")[1].startswith(("Put", "Create", "Delete", "Update", "Send", "Start", "Assume")) for a in actions)
    assert len(sts.assumed[0]["Policy"]) <= 2048


def test_org_role_takes_one_profile() -> None:
    import argparse

    import pytest

    import aws_multi
    import hopscope

    with pytest.raises(SystemExit):
        aws_multi.accounts_for(argparse.Namespace(profiles="a,b", org_role="HopscopeReadOnly", accounts=None, regions="eu-west-1"))
    from test_multi_cli import ns
    with pytest.raises(SystemExit):
        hopscope.check_collect(ns(profile="a,b", org_role="HopscopeReadOnly"))


def test_a_region_where_every_collector_failed_says_so(monkeypatch, capsys, tmp_path: Path) -> None:
    import pytest

    import aws_accounts
    import aws_inventory
    import aws_multi
    import aws_timeseries

    acct = "7" * 12

    class Boom:
        def client(self, *a, **k):
            raise PermissionError(f"AccessDenied for arn:aws:iam::{acct}:role/x")

    monkeypatch.setattr(aws_inventory, "collect_global", lambda s: {"cdn": [], "s3": {"count": 0}})
    monkeypatch.setattr(aws_inventory, "collect_cost", lambda s: [])
    monkeypatch.setattr(aws_timeseries, "daily_cost", lambda s: [])
    a = aws_accounts.Account("kestrel-prod", lambda r: Boom())
    parts, _, skipped = aws_multi.collect_all([a], lambda x: ["eu-west-1"], None)
    assert not parts and skipped[0]["why"].startswith("all collectors failed") and acct not in json.dumps(skipped)
    monkeypatch.setattr(aws_multi, "accounts_for", lambda args: [a])
    with pytest.raises(SystemExit) as e:
        aws_multi.main(["--regions", "eu-west-1,us-east-1", "--out", str(tmp_path / "a.json"), "--ts-out", str(tmp_path / "t.json")])
    assert "all collectors failed" in str(e.value) and not (tmp_path / "a.json").exists()


def test_time_series_are_aligned_on_the_hour() -> None:
    import aws_merge

    a = {"start": "2026-11-01T00:00:00+00:00", "labels": {"lb": "a"}, "series": {"req": [1.0, 2.0, 3.0]}, "byService": []}
    b = {"start": "2026-11-01T01:00:00+00:00", "labels": {"lb": "b"}, "series": {"req": [10.0, 20.0, 30.0]}, "byService": []}
    parts = [{"region": "eu-west-1"}, {"region": "us-east-1"}]
    got = aws_merge.merge_ts([a, b], parts, [])
    assert got["start"] == "2026-11-01T01:00:00+00:00" and got["series"]["req"] == [12.0, 23.0]


def test_kubectl_rows_are_their_own_scope(monkeypatch) -> None:
    import aws_eks
    import aws_multi

    monkeypatch.setattr(aws_eks, "collect_kube", lambda ctxs: {"ecs": [{"cluster": "shop", "n": f"api-{ctxs[0]}"}], "lb": []})
    parts = [{"account": "kestrel-prod", "region": "eu-west-1", "inv": {"ecs": []}, "ts": {}}]
    aws_multi.add_kube(parts, ["prod-eks", "stage-eks"], "kestrel-prod")
    assert [(p["account"], p["region"]) for p in parts[1:]] == [("kestrel-prod", "kube:prod-eks"), ("kestrel-prod", "kube:stage-eks")]
    assert parts[0]["inv"]["ecs"] == []


def test_lambda_prefix_guess_needs_three_functions_and_generic_folders_never_place() -> None:
    import linker_lambda

    files = [{"p": "handler/main.py", "x": []}, {"p": "auth/main.py", "x": []}]
    code = {"base": ".", "services": ["handler", "auth"], "files": files}
    fns = ["auth-handler", "kestrel-auth", "billing", "kestrel-orders-api"]
    got = linker_lambda.lambda_links(code, {"lambda": [{"n": n} for n in fns]}, {})
    assert "auth-handler" not in got or got["auth-handler"]["code"] != "handler"
    assert all(p["code"] != "handler" for p in got.values())
    two = linker_lambda.lambda_links(code, {"lambda": [{"n": "kestrel-auth"}, {"n": "kestrel-billing"}]}, {})
    assert two == {}  # with two functions, "kestrel" is not yet a shared prefix
