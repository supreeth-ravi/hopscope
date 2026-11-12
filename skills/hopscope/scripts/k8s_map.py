# skills/hopscope/scripts/k8s_map.py
"""Kubernetes objects → aws.json-shaped rows, shared by design mode (manifests, Kustomize, Helm: iac_k8s.py) and the
live cluster read (aws_eks.py), so the linker, rules, drift and console treat them like ECS services.

Workloads (Deployment, StatefulSet, DaemonSet, CronJob, Job) become ecs rows: cluster = namespace (or "k8s"), n = the
workload name, desired = replicas, images by repository name only, refs from env values and ConfigMaps through the
iac_common.safe_ref allowlist (Secrets are never read). Services, Ingresses and Gateway API HTTPRoutes become lb rows
whose routes reach the workloads their selectors pick; a HorizontalPodAutoscaler adds its min/max to the row.
Database, cache and search images with no build of ours become data rows, as in compose files."""
from __future__ import annotations

import re
from typing import Any

import iac_common as c

WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job")
KINDS = {*WORKLOADS, "Service", "Ingress", "IngressClass", "HTTPRoute", "ConfigMap", "HorizontalPodAutoscaler"}
OFF_THE_SHELF = re.compile(r"^(nginx|busybox|alpine|curl|envoy|pause|prometheus|grafana|loki|jaeger[\w-]*|otel[\w-]*|opentelemetry-collector[\w-]*|"
                           r"ingress-nginx|cert-manager[\w-]*|fluent-?bit|fluentd|traefik|haproxy|istio[\w-]*|linkerd[\w-]*|kube-[\w-]+|coredns|"
                           r"rabbitmq|zookeeper|kafka|cp-kafka|mailhog|mailpit|localstack|minio|vault|consul)$", re.I)
PUBLIC_REGISTRY = re.compile(r"^(registry\.k8s\.io|k8s\.gcr\.io|quay\.io|mcr\.microsoft\.com|public\.ecr\.aws|docker\.io/(bitnami|library))/", re.I)
SOURCE = "kubernetes"
WORD = re.compile(r"[A-Za-z0-9._ */,?@-]{1,60}")  # a schedule or controller name: nothing that could carry markup or a secret


def word(v: Any) -> str:
    t = str(v or "").strip() if isinstance(v, (str, int)) else ""
    return t if WORD.fullmatch(t) else ""


def _d(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _l(v: Any) -> list:
    return v if isinstance(v, list) else []


def _s(v: Any) -> str:
    return v if isinstance(v, str) else ""


def name_of(obj: dict) -> str:
    return _s(_d(obj.get("metadata")).get("name"))


def ns_of(obj: dict, default: str = "") -> str:
    return _s(_d(obj.get("metadata")).get("namespace")) or default


def pod_template(obj: dict) -> dict:
    spec = _d(obj.get("spec"))
    if obj.get("kind") == "CronJob":
        spec = _d(_d(spec.get("jobTemplate")).get("spec"))
    return _d(spec.get("template"))


def containers(obj: dict) -> list[dict]:
    return [x for x in _l(_d(pod_template(obj).get("spec")).get("containers")) if isinstance(x, dict)]


def pod_labels(obj: dict) -> dict:
    return {str(k): str(v) for k, v in _d(_d(pod_template(obj).get("metadata")).get("labels")).items()}


def _cm_index(objs: list[dict], ns: Any) -> dict[tuple[str, str], dict[str, str]]:
    """ConfigMaps by (namespace, name), with the namespace worked out exactly as for the workloads that refer to them."""
    return {(ns(o), name_of(o)): {str(k): str(v) for k, v in _d(o.get("data")).items() if not isinstance(v, (dict, list))}
            for o in objs if o.get("kind") == "ConfigMap"}


def env_values(obj: dict, ns: str, cms: dict[tuple[str, str], dict[str, str]]) -> dict[str, str]:
    """Literal env values and ConfigMap values a workload's containers see. secretKeyRef and secretRef are skipped:
    a Secret's value is never read."""
    out: dict[str, str] = {}
    for ct in containers(obj):
        for ref in _l(ct.get("envFrom")):
            cm = _d(_d(ref).get("configMapRef"))
            prefix = _s(_d(ref).get("prefix"))
            for k, v in cms.get((ns, _s(cm.get("name"))), {}).items():
                out[prefix + k] = v
        for e in _l(ct.get("env")):
            e = _d(e)
            key = _s(e.get("name"))
            if not key:
                continue
            if "value" in e and not isinstance(e["value"], (dict, list)):
                out[key] = "" if e["value"] is None else str(e["value"])
            ref = _d(_d(e.get("valueFrom")).get("configMapKeyRef"))
            if ref:
                val = cms.get((ns, _s(ref.get("name"))), {}).get(_s(ref.get("key")))
                if val is not None:
                    out[key] = val
    return out


def _replicas(obj: dict) -> tuple[int, bool]:
    spec = _d(obj.get("spec"))
    if obj.get("kind") in ("Deployment", "StatefulSet") and "replicas" in spec:
        n = c.as_int(spec.get("replicas"), -1)
        return (n, True) if n >= 0 else (1, False)
    if obj.get("kind") == "Job":
        return max(1, c.as_int(spec.get("parallelism"), 1)), False
    return 1, False


def _running(obj: dict, desired: int) -> int:
    """Live objects carry a status: ready replicas (Deployment, StatefulSet), ready pods (DaemonSet), active jobs (CronJob, Job)."""
    st = _d(obj.get("status"))
    if not st:
        return desired
    kind = obj.get("kind")
    if kind == "DaemonSet":
        return c.as_int(st.get("numberReady"), 0)
    if kind in ("CronJob", "Job"):
        return len(_l(st.get("active"))) if kind == "CronJob" else c.as_int(st.get("active"), 0)
    return c.as_int(st.get("readyReplicas"), 0)


def own_image(images: list[str], build: str | None) -> bool:
    """Ours unless every image is a well-known off-the-shelf one (nginx, an OpenTelemetry collector, a data store)."""
    if build is not None or not images:
        return True
    return not all(PUBLIC_REGISTRY.match(i) or OFF_THE_SHELF.match(c.image_engine(i)) or c.infra_kind(i) for i in images)


def build_for(images: list[str], builds: dict[str, str]) -> str | None:
    for i in images:
        bare = i.split("@")[0]
        bare = bare.rsplit(":", 1)[0] if ":" in bare.rsplit("/", 1)[-1] else bare
        for k in (bare, c.image_engine(i)):
            if k in builds:
                return builds[k]
    return None


def _selects(sel: dict, labels: dict) -> bool:
    return bool(sel) and all(labels.get(str(k)) == str(v) for k, v in sel.items())


def _ports(svc: dict) -> list[int]:
    return [p for x in _l(_d(svc.get("spec")).get("ports")) if (p := c.as_int(_d(x).get("port"), 0)) > 0]


class Mapper:
    def __init__(self, objs: list[tuple[dict, str]], builds: dict[str, str] | None, default_ns: str, live: bool) -> None:
        self.objs = [(o, src) for o, src in objs if isinstance(o, dict) and o.get("kind") in KINDS and name_of(o)]
        self.builds, self.default_ns, self.live = builds or {}, default_ns, live
        plain = [o for o, _ in self.objs]
        self.cms = _cm_index(plain, self.ns)
        self.services = [(o, src) for o, src in self.objs if o.get("kind") == "Service"]
        self.known: dict[str, dict[str, set[int]]] = {}  # namespace -> service -> ports: a host name only means something in its namespace
        for svc, _ in self.services:
            self.known.setdefault(self.ns(svc), {})[name_of(svc)] = set(_ports(svc))
        self.part: dict[str, list] = {"ecs": [], "lb": [], "rds": [], "cache": [], "search": []}
        self.pods: dict[tuple[str, str], dict] = {}  # (ns, workload) -> pod labels, for workloads that became ecs rows
        self.data: set[tuple[str, str]] = set()  # (ns, service name) of services in front of a data store

    def ns(self, o: dict) -> str:
        return ns_of(o, self.default_ns) or "k8s"

    def workloads(self) -> None:
        hpa = {(self.ns(o), _s(_d(_d(o.get("spec")).get("scaleTargetRef")).get("kind")) or "Deployment",
                 _s(_d(_d(o.get("spec")).get("scaleTargetRef")).get("name"))): _d(o.get("spec"))
               for o, _ in self.objs if o.get("kind") == "HorizontalPodAutoscaler"}
        for o, src in self.objs:
            if o.get("kind") not in WORKLOADS:
                continue
            ns, name = self.ns(o), name_of(o)
            images = [i for ct in containers(o) if (i := _s(ct.get("image")))]
            build = build_for(images, self.builds)
            kind = c.infra_kind(images[0]) if build is None and len(images) == 1 else None
            if kind:
                self._datastore(o, kind, ns, name, images[0], src)
                continue
            desired, literal = _replicas(o)
            scale = hpa.get((ns, str(o.get("kind")), name))
            if scale and not literal:
                desired = max(1, c.as_int(scale.get("minReplicas"), 1))
            row = c.ecs_row(ns, name, desired=desired, images=images, env_vars=env_values(o, ns, self.cms), build=build, build_from=src,
                            source=SOURCE, known_hosts=self.known.get(ns, {}), desired_set=literal)
            row.update(ownImage=own_image(images, build), workload=o.get("kind"), containers=[_s(ct.get("name")) or name for ct in containers(o)])
            if self.live:
                row["running"] = _running(o, desired)
            if scale:
                row["hpa"] = {"min": c.as_int(scale.get("minReplicas"), 1), "max": c.as_int(scale.get("maxReplicas"), 0)}
            if o.get("kind") == "CronJob":
                row["schedule"] = word(_d(o.get("spec")).get("schedule"))
            self.part["ecs"].append(row)
            self.pods[(ns, name)] = pod_labels(o)

    def _datastore(self, o: dict, kind: str, ns: str, name: str, image: str, src: str) -> None:
        labels = pod_labels(o)
        fronts = [name_of(s) for s, _ in self.services if self.ns(s) == ns and _selects(_d(_d(s.get("spec")).get("selector")), labels)]
        endpoint = fronts[0] if fronts else name
        self.data.update((ns, f) for f in fronts)
        base = {"cluster": ns, "src": src, "endpoint": endpoint, "source": SOURCE}
        if kind == "rds":
            self.part["rds"].append({**c.rds_row(name, c.image_engine(image), endpoint=endpoint), **base, "env": c.env_of(f"{ns}/{name}")})
        elif kind == "cache":
            self.part["cache"].append({**c.cache_row(name, c.image_engine(image)), **base})
        else:
            self.part["search"].append({"n": name, **base})

    def behind(self, ns: str, service: str) -> list[str]:
        svc = next((s for s, _ in self.services if self.ns(s) == ns and name_of(s) == service), None)
        sel = _d(_d(svc.get("spec")).get("selector")) if svc else {}
        return [f"{ns}/{w}" for (wns, w), labels in self.pods.items() if wns == ns and _selects(sel, labels)]

    def service_rows(self) -> None:
        internal: dict[str, dict] = {}
        for s, _ in self.services:
            ns, name, spec = self.ns(s), name_of(s), _d(s.get("spec"))
            if (ns, name) in self.data or spec.get("type") == "ExternalName":
                continue
            targets = self.behind(ns, name)
            selectorless = not _d(spec.get("selector"))
            if spec.get("type") in ("LoadBalancer", "NodePort"):
                lb = c.lb_row(name, internal=self._internal_lb(s), source=SOURCE)
                lb["k8s"] = f'Service {spec.get("type")}'
                self.part["lb"].append(lb)
            else:
                lb = internal.get(ns) or c.lb_row(f"{ns} (cluster services)", internal=True, source=SOURCE)
                lb["k8s"] = "Service ClusterIP"
                internal[ns] = lb
            for port in _ports(s) or [80]:
                when = f"service {name}:{port}" if lb["n"] != name else f"port {port}"
                lb["routes"].append(c.route(when, name, targets, port=port, tg_type="external" if selectorless else "ip"))
        self.part["lb"].extend(lb for lb in internal.values() if lb["routes"])

    @staticmethod
    def _internal_lb(s: dict) -> bool:
        ann = {str(k).lower(): str(v).lower() for k, v in _d(_d(s.get("metadata")).get("annotations")).items()}
        return any(v == "internal" or (k.endswith("load-balancer-internal") and v == "true") for k, v in ann.items() if "scheme" in k or "internal" in k)

    def ingress_rows(self) -> None:
        classes = {name_of(o): word(_d(o.get("spec")).get("controller")) for o, _ in self.objs if o.get("kind") == "IngressClass"}
        for o, _ in self.objs:
            if o.get("kind") != "Ingress":
                continue
            ns, spec = self.ns(o), _d(o.get("spec"))
            ann = {str(k): str(v) for k, v in _d(_d(o.get("metadata")).get("annotations")).items()}
            cls = _s(spec.get("ingressClassName")) or ann.get("kubernetes.io/ingress.class", "")
            lb = c.lb_row(name_of(o), internal=ann.get("alb.ingress.kubernetes.io/scheme") == "internal", source=SOURCE)
            lb.update(k8s="Ingress", ingressClass=word(cls), controller=classes.get(cls, ""))
            port = 443 if _l(spec.get("tls")) else 80
            default = _d(spec.get("defaultBackend") or spec.get("backend"))
            for rule in _l(spec.get("rules")):
                host = _s(_d(rule).get("host"))
                for p in _l(_d(_d(rule).get("http")).get("paths")):
                    self._ingress_route(lb, ns, host, _s(_d(p).get("path")) or "/", _d(_d(p).get("backend")), port)
            if default:
                self._ingress_route(lb, ns, "", "", default, port)
            self.part["lb"].append(lb)

    def _ingress_route(self, lb: dict, ns: str, host: str, path: str, backend: dict, port: int) -> None:
        svc = _s(_d(backend.get("service")).get("name")) or _s(backend.get("serviceName"))
        if not svc:
            return
        when = " & ".join(x for x in (f"host-header={host}" if host else "", f"path-pattern={path}" if path else "") if x) or "default"
        lb["routes"].append(c.route(when, svc, self.behind(ns, svc), port=port))

    def httproute_rows(self) -> None:
        gateways: dict[tuple[str, str], dict] = {}
        for o, _ in self.objs:
            if o.get("kind") != "HTTPRoute":
                continue
            ns, spec = self.ns(o), _d(o.get("spec"))
            parents = [_s(_d(p).get("name")) for p in _l(spec.get("parentRefs"))] or [name_of(o)]
            gw = gateways.get((ns, parents[0]))
            if gw is None:
                gw = c.lb_row(f"{parents[0]} (gateway)", source=SOURCE)
                gw["k8s"] = "Gateway API"
                gateways[(ns, parents[0])] = gw
            hosts = [h for h in _l(spec.get("hostnames")) if isinstance(h, str)] or [""]
            for rule in _l(spec.get("rules")):
                paths = [_s(_d(_d(m).get("path")).get("value")) for m in _l(_d(rule).get("matches"))] or [""]
                for ref in _l(_d(rule).get("backendRefs")):
                    svc = _s(_d(ref).get("name"))
                    for host in hosts:
                        for path in paths:
                            self._ingress_route(gw, ns, host, path, {"service": {"name": svc}}, 443)
        self.part["lb"].extend(gateways.values())


def map_objects(objs: list[tuple[dict, str]], builds: dict[str, str] | None = None, default_ns: str = "", live: bool = False) -> dict:
    """[(object, source file)] → {"ecs", "lb", "rds", "cache", "search"} rows. builds maps an image (or its repository name)
    to the code folder that builds it; live objects carry a status, which gives the running count."""
    m = Mapper(objs, builds, default_ns, live)
    m.workloads()
    m.service_rows()
    m.ingress_rows()
    m.httproute_rows()
    return c.mask_numbers(m.part)
