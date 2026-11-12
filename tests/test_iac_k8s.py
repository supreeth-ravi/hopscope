"""Kubernetes in design mode (iac_k8s.py): plain manifests, Kustomize bases and overlays, Helm charts without helm,
build folders from skaffold, compose and Dockerfiles, and file containment."""
from __future__ import annotations

from pathlib import Path

import iac_k8s
import iac_scan

DEPLOY = """apiVersion: apps/v1
kind: Deployment
metadata: {{name: {name}}}
spec:
  replicas: {replicas}
  selector: {{matchLabels: {{app: {name}}}}}
  template:
    metadata: {{labels: {{app: {name}}}}}
    spec:
      containers: [{{name: main, image: {image}}}]
---
apiVersion: v1
kind: Service
metadata: {{name: {name}}}
spec: {{selector: {{app: {name}}}, ports: [{{port: 80}}]}}
"""


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def _ecs(part: dict) -> dict[str, dict]:
    return {f'{r["cluster"]}/{r["n"]}': r for r in part["ecs"]}


def test_plain_manifests_and_skaffold_builds(tmp_path: Path) -> None:
    _write(tmp_path, {
        "k8s/brew-api.yaml": DEPLOY.format(name="brew-api", replicas=2, image="brew-api"),
        "k8s/menu.yml": DEPLOY.format(name="menu-api", replicas=1, image="ghcr.io/kestrel/menu-api:2"),
        "skaffold.yaml": "apiVersion: skaffold/v4beta6\nkind: Config\nbuild:\n  artifacts:\n  - {image: brew-api, context: services/brew}\n",
        "services/brew/Dockerfile": "FROM python:3.12\n",
        "menu-api/Dockerfile": "FROM node:20\n",
        "notes.yaml": "title: not kubernetes\n",
    })
    part, files, skipped = iac_k8s.scan(tmp_path)
    rows = _ecs(part)
    assert rows["k8s/brew-api"]["build"] == "services/brew" and rows["k8s/brew-api"]["desired"] == 2
    assert rows["k8s/menu-api"]["build"] == "menu-api"  # a Dockerfile folder named like the image's repository
    assert sorted(files) == ["k8s/brew-api.yaml", "k8s/menu.yml", "skaffold.yaml"] and skipped == []


def test_compose_build_contexts_name_images(tmp_path: Path) -> None:
    _write(tmp_path, {
        "deploy/app.yaml": DEPLOY.format(name="orders", replicas=2, image="kestrel/orders:1"),
        "docker-compose.yml": "services:\n  orders:\n    image: kestrel/orders\n    build: ./src/orders\n",
    })
    part, _, _ = iac_k8s.scan(tmp_path)
    assert _ecs(part)["k8s/orders"]["build"] == "src/orders"


KUSTOMIZE = {
    "k8s/base/kustomization.yaml": "resources:\n- deploy.yaml\n- config.yaml\n",
    "k8s/base/deploy.yaml": DEPLOY.format(name="brew-api", replicas=1, image="brew-api") + """---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: web}
spec:
  rules: [{host: shop.kestrel.example, http: {paths: [{path: /, backend: {service: {name: brew-api, port: {number: 80}}}}]}}]
""",
    "k8s/base/config.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: brew-config}\ndata: {ORDERS_QUEUE: kestrel-orders}\n",
    "k8s/overlays/prod/kustomization.yaml": """namespace: kestrel-prod
namePrefix: prod-
resources:
- ../../base
- https://github.com/example/remote//base?ref=v1
patches:
- path: replicas.yaml
patchesStrategicMerge:
- env.yaml
images:
- {name: brew-api, newName: 123456789012.dkr.ecr.eu-west-1.amazonaws.com/brew-api, newTag: "7"}
""",
    "k8s/overlays/prod/replicas.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: brew-api}\nspec: {replicas: 4}\n",
    "k8s/overlays/prod/env.yaml": """apiVersion: apps/v1
kind: Deployment
metadata: {name: brew-api}
spec:
  template:
    spec:
      containers:
      - name: main
        envFrom: [{configMapRef: {name: brew-config}}]
""",
    "k8s/overlays/staging/kustomization.yaml": "namespace: kestrel-staging\nresources: [../../base]\nreplicas: [{name: brew-api, count: 1}]\n",
}


def test_kustomize_overlays_are_rendered_from_what_is_written(tmp_path: Path) -> None:
    _write(tmp_path, KUSTOMIZE)
    part, files, skipped = iac_k8s.scan(tmp_path)
    rows = _ecs(part)
    assert set(rows) == {"kestrel-prod/prod-brew-api", "kestrel-staging/brew-api"}  # the base is not read on its own
    prod = rows["kestrel-prod/prod-brew-api"]
    assert prod["desired"] == 4 and prod["env"] == "prod" and prod["ownImage"] is True and prod["images"] == ["brew-api"]
    assert {"k": "ORDERS_QUEUE", "v": "kestrel-orders"} in prod["refs"]  # the ConfigMap was prefixed too, and the reference followed it
    assert rows["kestrel-staging/brew-api"]["env"] == "staging"
    ing = next(lb for lb in part["lb"] if lb["n"] == "prod-web")
    assert ing["routes"][0]["tg"] == "prod-brew-api" and ing["routes"][0]["services"] == ["kestrel-prod/prod-brew-api"]
    assert "k8s/base/deploy.yaml" in files
    assert any("remote" in s["error"] for s in skipped)


def test_helm_chart_simple_values_only(tmp_path: Path) -> None:
    _write(tmp_path, {
        "charts/kestrel/Chart.yaml": "apiVersion: v2\nname: kestrel\nversion: 0.1.0\n",
        "charts/kestrel/values.yaml": "api:\n  name: brew-api\n  replicas: 3\n  image: brew-api\nqueue: kestrel-orders\n",
        "charts/kestrel/templates/_helpers.tpl": '{{- define "kestrel.fullname" -}}{{ .Release.Name }}{{- end }}\n',
        "charts/kestrel/templates/api.yaml": """{{- if .Values.api.enabled }}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Values.api.name }}
  labels:
    {{- include "kestrel.labels" . | nindent 4 }}
spec:
  replicas: {{ .Values.api.replicas }}
  selector: {matchLabels: {app: {{ .Values.api.name }}}}
  template:
    metadata: {labels: {app: {{ .Values.api.name }}}}
    spec:
      containers:
      - name: api
        image: "{{ .Values.api.image }}:{{ .Chart.AppVersion }}"
        env:
        - name: ORDERS_QUEUE
          value: {{ .Values.queue | quote }}
        - name: QUEUE_NAME
          value: {{ .Values.queue }}
{{- end }}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ include "kestrel.fullname" . }}
spec:
  selector: {app: {{ .Values.api.name }}}
  ports: [{port: 80}]
""",
        "charts/kestrel/templates/NOTES.txt": "Thanks {{ .Release.Name }}\n",
    })
    part, files, _ = iac_k8s.scan(tmp_path)
    api = _ecs(part)["k8s/brew-api"]
    assert api["desired"] == 3 and {"k": "QUEUE_NAME", "v": "kestrel-orders"} in api["refs"]
    assert not any(r["k"] == "ORDERS_QUEUE" for r in api["refs"])  # a line with other template logic is skipped
    route = part["lb"][0]["routes"][0]
    assert route["tg"] == "kestrel" and route["services"] == ["k8s/brew-api"]
    assert "charts/kestrel/templates/api.yaml" in files


def test_helm_prefers_pre_rendered_output(tmp_path: Path) -> None:
    _write(tmp_path, {
        "charts/kestrel/Chart.yaml": "name: kestrel\n",
        "charts/kestrel/values.yaml": "name: from-template\n",
        "charts/kestrel/templates/api.yaml": DEPLOY.format(name="{{ .Values.name }}", replicas=1, image="x"),
        "charts/kestrel/rendered/all.yaml": DEPLOY.format(name="from-rendered", replicas=2, image="x"),
    })
    part, _, _ = iac_k8s.scan(tmp_path)
    assert set(_ecs(part)) == {"k8s/from-rendered"}


def test_same_object_from_several_sources_is_read_once(tmp_path: Path) -> None:
    _write(tmp_path, {
        "kubernetes-manifests/kustomization.yaml": "resources: [api.yaml]\n",
        "kubernetes-manifests/api.yaml": DEPLOY.format(name="brew-api", replicas=2, image="brew-api"),
        "release/all.yaml": DEPLOY.format(name="brew-api", replicas=9, image="gcr.io/x/brew-api:1"),
        "helm/Chart.yaml": "name: shop\n",
        "helm/templates/api.yaml": DEPLOY.format(name="brew-api", replicas=5, image="brew-api"),
    })
    part, _, _ = iac_k8s.scan(tmp_path)
    assert [(r["n"], r["desired"]) for r in part["ecs"]] == [("brew-api", 2)]


def test_containment_symlinks_escapes_and_size(tmp_path: Path) -> None:
    outside = _write(tmp_path / "outside", {"leak.yaml": DEPLOY.format(name="leaked", replicas=1, image="x")})
    repo = _write(tmp_path / "repo", {
        "k8s/kustomization.yaml": "resources: [../../outside/leak.yaml, /etc/passwd, big.yaml, link.yaml]\n",
        "k8s/big.yaml": DEPLOY.format(name="big", replicas=1, image="x") + "#" * (iac_k8s.MAX_BYTES + 1),
    })
    (repo / "k8s" / "link.yaml").symlink_to(outside / "leak.yaml")
    (repo / "linked.yaml").symlink_to(outside / "leak.yaml")
    part, files, skipped = iac_k8s.scan(repo)
    assert part["ecs"] == [] and files == []
    assert {s["file"] for s in skipped} >= {"k8s/kustomization.yaml"}


def test_kustomize_cycles_and_bad_yaml_do_not_stop_the_scan(tmp_path: Path) -> None:
    _write(tmp_path, {
        "a/kustomization.yaml": "resources: [../b]\n",
        "b/kustomization.yaml": "resources: [../a, ok.yaml]\n",
        "b/ok.yaml": DEPLOY.format(name="menu", replicas=1, image="menu"),
        "c/broken.yaml": "apiVersion: v1\nkind: Service\nmetadata: {name: [\n",
    })
    part, _, skipped = iac_k8s.scan(tmp_path)
    assert [r["n"] for r in part["ecs"]] == ["menu"]
    assert any(s["file"] == "c/broken.yaml" for s in skipped)


def test_iac_scan_merges_kubernetes_rows(tmp_path: Path) -> None:
    _write(tmp_path, {"k8s/api.yaml": DEPLOY.format(name="brew-api", replicas=2, image="brew-api"),
                      "docker-compose.yml": "services:\n  brew-api:\n    build: ./api\n"})
    inv = iac_scan.scan(tmp_path, "design")
    sources = {(r["cluster"], r["n"], r["source"]) for r in inv["ecs"]}
    assert ("k8s", "brew-api", "kubernetes") in sources and ("compose", "brew-api", "compose") in sources
    assert "k8s/api.yaml" in inv["sources"]
    assert _ecs({"ecs": inv["ecs"]})["k8s/brew-api"]["build"] == "api"


def test_design_pipeline_on_a_kubernetes_repo(tmp_path: Path) -> None:
    import json
    import subprocess
    import sys

    repo = _write(tmp_path / "kestrel", {
        "brew/main.py": "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/api/brews')\ndef brews():\n    return []\n",
        "brew/pyproject.toml": "[project]\nname = 'brew'\n",
        "skaffold.yaml": "apiVersion: skaffold/v4beta6\nkind: Config\nbuild:\n  artifacts:\n  - {image: brew-api, context: brew}\n",
        "k8s/app.yaml": DEPLOY.format(name="brew-api", replicas=2, image="brew-api"),
    })
    work = tmp_path / "work"
    script = Path(__file__).resolve().parents[1] / "skills" / "hopscope" / "scripts" / "hopscope.py"
    subprocess.run([sys.executable, str(script), "design", "--repo", str(repo), "--workdir", str(work)], check=True, capture_output=True)
    model = json.loads((work / "data" / "model.json").read_text())
    row = next(r for r in model["infra"] if r["kind"] == "ecs")
    assert (row["n"], row["src"], row["sys"], row["workload"]) == ("brew-api", "kubernetes", "brew", "Deployment")
    assert model["streams"] == {} and model["consumers"] == {}
    links = json.loads((work / "data" / "links.json").read_text())
    assert links["placement"]["k8s/brew-api"]["evidence"] == ["skaffold.yaml (+1 more) builds brew"]


def test_kustomize_replicas_change_only_workloads(tmp_path: Path) -> None:
    import iac_kustomize

    _write(tmp_path, {"k/kustomization.yaml": "resources: [app.yaml]\nreplicas: [{name: brew-api, count: 5}]\n",
                      "k/app.yaml": DEPLOY.format(name="brew-api", replicas=1, image="brew-api")})
    objs = iac_kustomize.render(tmp_path.resolve(), "k", set(), [])
    by_kind = {o["kind"]: o for o, _ in objs}
    assert by_kind["Deployment"]["spec"]["replicas"] == 5 and "replicas" not in by_kind["Service"]["spec"]


def test_skipped_reasons_never_show_absolute_local_paths(tmp_path: Path) -> None:
    _write(tmp_path, {"k/kustomization.yaml": "resources: [/Users/someone/secret/app.yaml, ~/x.yaml, ../../../etc/hosts]\npatches: [{path: /home/someone/p.yaml}]\n"})
    _, _, skipped = iac_k8s.scan(tmp_path)
    text = str(skipped)
    assert "/Users/" not in text and "/home/" not in text and "~/" not in text and "app.yaml" in text
