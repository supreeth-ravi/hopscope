"""Kubernetes objects → aws.json rows (k8s_map.py), shared by design mode and the live cluster read."""
from __future__ import annotations

import yaml

import k8s_map

MANIFEST = """
apiVersion: v1
kind: ConfigMap
metadata: {name: brew-config, namespace: kestrel}
data:
  ORDERS_QUEUE: kestrel-orders
  ROAST_DB_HOST: roast-db
  API_TOKEN: abc123def456ghi789jkl
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: brew-api, namespace: kestrel}
spec:
  replicas: 3
  selector: {matchLabels: {app: brew-api}}
  template:
    metadata: {labels: {app: brew-api, tier: web}}
    spec:
      containers:
      - name: api
        image: 123456789012.dkr.ecr.eu-west-1.amazonaws.com/kestrel/brew-api:1.4
        envFrom:
        - configMapRef: {name: brew-config}
        - secretRef: {name: brew-secrets}
        env:
        - {name: CACHE_HOST, value: "beans-cache:6379"}
        - {name: DB_PASSWORD, valueFrom: {secretKeyRef: {name: brew-secrets, key: pw}}}
        - {name: QUEUE_NAME, valueFrom: {configMapKeyRef: {name: brew-config, key: ORDERS_QUEUE}}}
---
apiVersion: apps/v1
kind: StatefulSet
metadata: {name: beans-cache, namespace: kestrel}
spec:
  selector: {matchLabels: {app: beans-cache}}
  template:
    metadata: {labels: {app: beans-cache}}
    spec: {containers: [{name: redis, image: "redis:7"}]}
---
apiVersion: v1
kind: Service
metadata: {name: beans-cache, namespace: kestrel}
spec: {selector: {app: beans-cache}, ports: [{port: 6379}]}
---
apiVersion: batch/v1
kind: CronJob
metadata: {name: roast-report, namespace: kestrel}
spec:
  schedule: "0 6 * * *"
  jobTemplate:
    spec:
      template:
        metadata: {labels: {app: roast-report}}
        spec: {containers: [{name: job, image: roast-report}]}
---
apiVersion: v1
kind: Service
metadata: {name: brew-api, namespace: kestrel}
spec:
  selector: {app: brew-api}
  ports: [{port: 80, targetPort: 8000}]
---
apiVersion: v1
kind: Service
metadata: {name: brew-public, namespace: kestrel}
spec:
  type: LoadBalancer
  selector: {app: brew-api}
  ports: [{port: 443}]
---
apiVersion: networking.k8s.io/v1
kind: IngressClass
metadata: {name: alb}
spec: {controller: ingress.k8s.aws/alb}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: kestrel-web, namespace: kestrel}
spec:
  ingressClassName: alb
  tls: [{hosts: [shop.kestrel.example]}]
  rules:
  - host: shop.kestrel.example
    http:
      paths:
      - {path: /api, pathType: Prefix, backend: {service: {name: brew-api, port: {number: 80}}}}
      - {path: /legacy, pathType: Prefix, backend: {service: {name: gone-api, port: {number: 80}}}}
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: {name: brew-api, namespace: kestrel}
spec:
  scaleTargetRef: {apiVersion: apps/v1, kind: Deployment, name: brew-api}
  minReplicas: 2
  maxReplicas: 10
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: {name: brew-route, namespace: kestrel}
spec:
  parentRefs: [{name: kestrel-gw}]
  hostnames: [api.kestrel.example]
  rules:
  - matches: [{path: {type: PathPrefix, value: /v2}}]
    backendRefs: [{name: brew-api, port: 80}]
"""


def _part(text: str = MANIFEST, **kw) -> dict:
    return k8s_map.map_objects([(d, "deploy/k8s/app.yaml") for d in yaml.safe_load_all(text) if d], **kw)


def test_workloads_become_ecs_rows() -> None:
    rows = {r["n"]: r for r in _part()["ecs"]}
    api = rows["brew-api"]
    assert api["cluster"] == "kestrel" and api["desired"] == 3 and api["desiredSet"] is True
    assert api["images"] == ["brew-api"] and api["ownImage"] is True and api["source"] == "kubernetes" and api["workload"] == "Deployment"
    assert api["hpa"] == {"min": 2, "max": 10} and api["buildFrom"] == "deploy/k8s/app.yaml"
    assert rows["roast-report"]["workload"] == "CronJob" and rows["roast-report"]["schedule"] == "0 6 * * *"


def test_env_refs_use_the_allowlist_and_never_secrets() -> None:
    api = next(r for r in _part()["ecs"] if r["n"] == "brew-api")
    refs = {r["k"]: r["v"] for r in api["refs"]}
    assert refs == {"ORDERS_QUEUE": "kestrel-orders", "QUEUE_NAME": "kestrel-orders", "ROAST_DB_HOST": "roast-db", "CACHE_HOST": "beans-cache:6379"}
    assert "abc123" not in str(api) and "DB_PASSWORD" not in str(api)


def test_datastore_images_become_data_rows_behind_their_service() -> None:
    part = _part()
    assert [(x["n"], x["endpoint"], x["cluster"]) for x in part["cache"]] == [("beans-cache", "beans-cache", "kestrel")]
    assert "beans-cache" not in {r["n"] for r in part["ecs"]}
    assert not any(r["tg"] == "beans-cache" for lb in part["lb"] for r in lb["routes"])


def test_services_and_ingress_route_to_workloads() -> None:
    lbs = {lb["n"]: lb for lb in _part()["lb"]}
    internal = lbs["kestrel (cluster services)"]
    assert internal["scheme"] == "internal" and internal["routes"][0]["when"] == "service brew-api:80"
    assert internal["routes"][0]["services"] == ["kestrel/brew-api"]
    assert lbs["brew-public"]["scheme"] == "internet-facing" and lbs["brew-public"]["routes"][0]["when"] == "port 443"
    ing = lbs["kestrel-web"]
    assert ing["k8s"] == "Ingress" and ing["controller"] == "ingress.k8s.aws/alb" and ing["ingressClass"] == "alb"
    by_when = {r["when"]: r for r in ing["routes"]}
    assert by_when["host-header=shop.kestrel.example & path-pattern=/api"]["services"] == ["kestrel/brew-api"]
    assert by_when["host-header=shop.kestrel.example & path-pattern=/legacy"]["services"] == []
    assert by_when["host-header=shop.kestrel.example & path-pattern=/api"]["port"] == 443


def test_gateway_api_httproute() -> None:
    gw = next(lb for lb in _part()["lb"] if lb["n"] == "kestrel-gw (gateway)")
    assert gw["routes"][0]["when"] == "host-header=api.kestrel.example & path-pattern=/v2" and gw["routes"][0]["services"] == ["kestrel/brew-api"]


def test_builds_link_images_to_code_folders() -> None:
    rows = {r["n"]: r for r in _part(builds={"roast-report": "jobs/roast-report"})["ecs"]}
    assert rows["roast-report"]["build"] == "jobs/roast-report" and rows["brew-api"]["build"] is None


def test_default_namespace_and_hpa_min_as_desired() -> None:
    text = """
kind: Deployment
metadata: {name: menu-api}
spec: {template: {metadata: {labels: {app: menu}}, spec: {containers: [{name: a, image: "nginx:1.27"}]}}}
---
kind: HorizontalPodAutoscaler
metadata: {name: menu-api}
spec: {scaleTargetRef: {kind: Deployment, name: menu-api}, minReplicas: 3, maxReplicas: 6}
"""
    row = _part(text)["ecs"][0]
    assert row["cluster"] == "k8s" and row["desired"] == 3 and row["desiredSet"] is False and row["ownImage"] is False


def test_live_objects_report_ready_replicas_and_mask_accounts() -> None:
    obj = {"kind": "Deployment", "metadata": {"name": "brew-api", "namespace": "kestrel"},
           "spec": {"replicas": 3, "template": {"spec": {"containers": [{"name": "a", "image": "123456789012.dkr.ecr.eu-west-1.amazonaws.com/brew-api:1"}]}}},
           "status": {"readyReplicas": 1}}
    row = k8s_map.map_objects([(obj, "")], live=True)["ecs"][0]
    assert row["running"] == 1 and row["desired"] == 3 and "123456789012" not in str(row)


def test_garbage_objects_are_ignored() -> None:
    objs = [({"kind": "Deployment"}, ""), ({"kind": "Secret", "metadata": {"name": "s"}, "data": {"pw": "x"}}, ""), ("text", ""),
            ({"kind": "Deployment", "metadata": {"name": "x"}, "spec": "bad"}, "")]
    part = k8s_map.map_objects(objs)  # type: ignore[arg-type]
    assert [r["n"] for r in part["ecs"]] == ["x"] and part["lb"] == []


def test_selectorless_services_are_not_reported_as_dead_routes() -> None:
    import rules

    text = "kind: Service\nmetadata: {name: legacy-db}\nspec: {ports: [{port: 5432}]}\n"
    inv = {"lb": _part(text)["lb"], "sqs": [], "ecs": []}
    assert inv["lb"][0]["routes"][0]["tgType"] == "external"
    assert rules.evaluate_design(inv, {}) == []


def test_single_replica_check_skips_jobs_daemonsets_and_autoscaled() -> None:
    import rules

    rows = {r["n"]: r for r in _part()["ecs"]}
    one = {**rows["brew-api"], "desired": 1}
    inv = {"lb": [], "sqs": [], "ecs": [rows["roast-report"], one, {**one, "n": "solo", "hpa": None}]}
    actions = rules.evaluate_design(inv, {})
    assert len(actions) == 1 and actions[0][2].startswith("solo:")


def test_configmaps_resolve_without_namespaces() -> None:
    text = """
kind: ConfigMap
metadata: {name: brew-config}
data: {ORDERS_QUEUE: kestrel-orders}
---
kind: Deployment
metadata: {name: brew-api}
spec:
  template:
    spec:
      containers:
      - name: a
        image: brew-api
        envFrom: [{configMapRef: {name: brew-config}}]
        env: [{name: QUEUE_NAME, valueFrom: {configMapKeyRef: {name: brew-config, key: ORDERS_QUEUE}}}]
"""
    refs = {r["k"]: r["v"] for r in _part(text)["ecs"][0]["refs"]}
    assert refs == {"ORDERS_QUEUE": "kestrel-orders", "QUEUE_NAME": "kestrel-orders"}


def test_hpa_kind_and_service_ports_per_namespace() -> None:
    text = """
kind: Deployment
metadata: {name: brew-api, namespace: shop}
spec: {template: {spec: {containers: [{name: a, image: brew-api, env: [{name: CACHE_HOST, value: "beans:7777"}]}]}}}
---
kind: StatefulSet
metadata: {name: brew-api, namespace: shop}
spec: {template: {spec: {containers: [{name: a, image: brew-db-sync}]}}}
---
kind: HorizontalPodAutoscaler
metadata: {name: brew-api, namespace: shop}
spec: {scaleTargetRef: {kind: StatefulSet, name: brew-api}, minReplicas: 4, maxReplicas: 8}
---
kind: Service
metadata: {name: beans, namespace: other}
spec: {selector: {app: beans}, ports: [{port: 7777}]}
"""
    rows = {r["workload"]: r for r in _part(text)["ecs"]}
    assert "hpa" not in rows["Deployment"] and rows["StatefulSet"]["hpa"] == {"min": 4, "max": 8}
    assert rows["Deployment"]["refs"] == []  # beans:7777 is a service in another namespace: not a known host here
