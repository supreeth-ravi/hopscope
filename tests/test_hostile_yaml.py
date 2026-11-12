"""Hostile YAML in a scanned repo: alias bombs ("billion laughs") and very deep nesting skip just that file, quickly,
and every other file is still read. Invented Kestrel fixtures."""
from __future__ import annotations

import time
from pathlib import Path

import iac_k8s
import iac_scan

GOOD = """apiVersion: apps/v1
kind: Deployment
metadata: {name: brew-api}
spec:
  template:
    metadata: {labels: {app: brew-api}}
    spec: {containers: [{name: api, image: brew-api}]}
"""


def bomb(top: str) -> str:
    lines = ['a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]']
    for prev, cur in zip("abcdefgh", "bcdefghi", strict=True):
        lines.append(f"{cur}: &{cur} [{','.join(['*' + prev] * 9)}]")
    return "\n".join(lines) + "\n" + top


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def _timed(fn, *a):
    t = time.monotonic()
    out = fn(*a)
    assert time.monotonic() - t < 10
    return out


def test_alias_bombs_in_kubernetes_files_are_skipped(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {
        "k8s/good.yaml": GOOD,
        "k8s/bomb.yaml": bomb("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\ndata: {K: *i}\n"),
        "overlay/kustomization.yaml": bomb("resources: *i\n"),
        "chart/Chart.yaml": "name: kestrel\n",
        "chart/values.yaml": bomb("x: *i\n"),
        "chart/templates/api.yaml": GOOD.replace("brew-api", "{{ .Values.x }}"),
    })
    part, _, skipped = _timed(iac_k8s.scan, repo)
    assert [r["n"] for r in part["ecs"]] == ["brew-api"]
    bad = {s["file"] for s in skipped}
    assert {"k8s/bomb.yaml", "overlay/kustomization.yaml"} <= bad


def test_deep_nesting_skips_only_that_file(tmp_path: Path) -> None:
    deep = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: deep}\ndata: " + "[" * 200_000 + "]" * 200_000 + "\n"
    repo = _repo(tmp_path, {"k8s/good.yaml": GOOD, "k8s/deep.yaml": deep, "k8s/kustomization.yaml": "resources: [good.yaml, deep.yaml]\n",
                            "deep/kustomization.yaml": "resources: " + "[" * 200_000 + "]" * 200_000 + "\n",
                            "docker-compose.yml": "services:\n  x: " + "[" * 200_000 + "]" * 200_000 + "\n",
                            "cfn/template.yaml": "Resources: " + "[" * 200_000 + "]" * 200_000 + "\nAWS::X\n"})
    inv = _timed(iac_scan.scan, repo, "design")
    assert [r["n"] for r in inv["ecs"]] == ["brew-api"]
    bad = {s["file"] for s in inv["skipped"]}
    assert {"k8s/deep.yaml", "deep/kustomization.yaml", "docker-compose.yml", "cfn/template.yaml"} <= bad


def test_compose_and_cloudformation_keep_anchors_but_never_expand_bombs(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {
        "docker-compose.yml": "x-env: &env {ORDERS_QUEUE: kestrel-orders}\n" + bomb(
            "services:\n  api:\n    build: ./api\n    environment: *env\n  bad:\n    build: ./bad\n    environment: {K: *i}\n    ports: *i\n    image: *i\n"),
        "cfn/template.yaml": bomb("Resources:\n  Q:\n    Type: AWS::SQS::Queue\n    Properties: {QueueName: *i}\n  Ok:\n    Type: AWS::SQS::Queue\n    Properties: {QueueName: kestrel-ok}\n"),
    })
    inv = _timed(iac_scan.scan, repo, "design")
    api = next(r for r in inv["ecs"] if r["n"] == "api")
    assert {"k": "ORDERS_QUEUE", "v": "kestrel-orders"} in api["refs"]  # an anchor used as compose files do still works
    assert len(str(inv)) < 100_000
