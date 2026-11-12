"""Lambda functions linked to the code folder they run (linker_lambda.py), so a serverless route traces through API Gateway
and its function. Names only: a folder is placed on the functions whose names hold all of the folder's name words."""
from __future__ import annotations

import linker_lambda
import rules


def code() -> dict:
    files = [{"p": "orders-api/app/main.py", "x": []}, {"p": "fulfil-worker/handler.py", "x": []}, {"p": "shop/main.py", "x": []}, {"p": "api/x.py", "x": []}]
    return {"base": ".", "services": ["orders-api", "fulfil-worker", "shop", "api"], "files": files}


def inv() -> dict:
    fns = ["kestrel-orders-api", "eu-west-1:kestrel-orders-api", "kestrel-fulfil-worker", "kestrel-image-resize", "shop-api-warmer", "shop-checkout"]
    return {"lambda": [{"n": n, "mem": 512, "inv": 10, "err": 0, "dur": 9, "region": "eu-west-1" if n.startswith("eu") else "us-east-1"} for n in fns]}


def test_folders_placed_on_functions_by_their_name_words() -> None:
    got = linker_lambda.lambda_links(code(), inv(), {})
    assert got["kestrel-orders-api"]["code"] == "orders-api" and got["eu-west-1:kestrel-orders-api"]["code"] == "orders-api"
    assert got["kestrel-fulfil-worker"]["code"] == "fulfil-worker"
    assert "kestrel-image-resize" not in got
    assert "shop-api-warmer" not in got and "shop-checkout" not in got  # two folders fit (shop, api) or the folder is a generic word
    assert all(0.6 <= p["confidence"] < 1 and p["evidence"] for p in got.values())


def test_folders_already_running_on_ecs_are_left_alone() -> None:
    got = linker_lambda.lambda_links(code(), inv(), {"prod/orders": {"code": "orders-api", "confidence": 1.0}})
    assert "kestrel-orders-api" not in got and "kestrel-fulfil-worker" in got


def test_overrides_place_or_ignore_a_function() -> None:
    got = linker_lambda.lambda_links(code(), inv(), {}, {"lambda:kestrel-image-resize": "shop", "lambda:kestrel-fulfil-worker": None})
    assert got["kestrel-image-resize"] == {"code": "shop", "confidence": 1.0, "evidence": ["set in hopscope.json"]}
    assert "kestrel-fulfil-worker" not in got


def test_model_rows_and_placement_for_linked_functions() -> None:
    links = {"placement": {}, "labels": {"orders-api": "Orders api"}, "lambdas": linker_lambda.lambda_links(code(), inv(), {})}
    i = {**inv(), "ecs": [], "ec2": [], "rds": []}
    rows, labels = rules.infra_entries(i, links)
    fn = next(r for r in rows if r["n"] == "kestrel-orders-api")
    assert fn["kind"] == "lambda" and fn["sys"] == "orders-api" and fn["key"] == "lambda:kestrel-orders-api" and fn["region"] == "us-east-1"
    assert "Lambda" in fn["size"] and not any(r["n"] == "kestrel-image-resize" for r in rows)


def test_console_traces_through_api_gateway_into_the_function() -> None:
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "skills" / "hopscope" / "assets" / "console.html").read_text()
    assert "source: 'apigw'" in html and "`API Gateway invokes the Lambda function ${t.compute[0]}.`" in html
    assert "isLambda(byName[t.compute[0]]) ? 'Lambda function'" in html and "lambda: 'Lambda function'" in html
