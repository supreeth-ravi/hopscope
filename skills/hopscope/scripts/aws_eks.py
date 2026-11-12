"""EKS and live Kubernetes, read-only and opt-in.

AWS side (always, with the inventory): eks:ListClusters/DescribeCluster and ListNodegroups/DescribeNodegroup into aws.json
`eks` (names, versions, node group sizes; no endpoints, ARNs or roles).

Cluster side, only when the user names a kubeconfig context (--kube-context / --kube-contexts): one
`kubectl --context <ctx> get deploy,sts,ds,svc,ing,hpa,cronjob -A -o json` and one `kubectl get events -A -o json`, run
as a list (no shell) with a timeout. Only the `get` verb is used and Secrets are never asked for. The objects go through
the same mapper as design mode (k8s_map), and allowlisted warning events from the last 72 hours become `deploys` event rows
rebuilt from fixed descriptions (k8s_events: the event text itself is never stored),
which history.failure_actions turns into actions (CrashLoopBackOff, ImagePullBackOff, FailedScheduling, OOMKilled).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

import k8s_events
import k8s_map
from aws_inventory import arn_name, iso, mask_text
from awsutil import paginate
from iac_common import mask_numbers

RESOURCES = "deploy,sts,ds,svc,ing,hpa,cronjob"
CONTEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_./@+-]{0,250}$")
TIMEOUT_S, EVENT_HOURS, MAX_EVENTS = 90, 72, 20
MAX_STDOUT = 200_000_000

class KubeError(Exception):
    """kubectl is missing, refused a context or failed; the message is masked."""


def collect_eks(s: Any) -> list[dict]:
    eks = s.client("eks")
    out = []
    for name in paginate(eks, "list_clusters", "clusters"):
        c = eks.describe_cluster(name=name)["cluster"]
        groups = []
        for ng in paginate(eks, "list_nodegroups", "nodegroups", clusterName=name):
            g = eks.describe_nodegroup(clusterName=name, nodegroupName=ng)["nodegroup"]
            sc = g.get("scalingConfig") or {}
            groups.append({"n": g.get("nodegroupName", ng), "status": g.get("status", ""), "types": list(g.get("instanceTypes") or []),
                           "capacity": g.get("capacityType", ""), "min": sc.get("minSize"), "max": sc.get("maxSize"), "desired": sc.get("desiredSize")})
        out.append({"n": c.get("name", name), "version": c.get("version", ""), "status": c.get("status", ""), "nodegroups": groups})
    return mask_numbers(out)


def contexts(flags: list[str] | None, joined: str | None) -> list[str]:
    """--kube-context a [--kube-context b] and --kube-contexts a,b, in order."""
    raw = [*(flags or []), *([joined] if joined else [])]
    return [c.strip() for item in raw for c in item.split(",") if c.strip()]


def short(ctx: str) -> str:
    """A context's readable name: an EKS ARN becomes its cluster name."""
    return mask_numbers(arn_name(ctx) if ctx.startswith("arn:") else ctx)[:60]


def kubectl(ctx: str, *args: str) -> dict:
    exe = shutil.which("kubectl")
    if not exe:
        raise KubeError("kubectl is not installed or not on PATH; live Kubernetes workloads are skipped")
    if not CONTEXT.match(ctx or ""):
        raise KubeError("refusing a kubeconfig context that is empty or not a plain name")
    if args[0] != "get" or any("secret" in a.lower() for a in args):
        raise KubeError("only read-only `kubectl get` of workloads and events is allowed")
    cmd = [exe, "--context", ctx, *args, "--request-timeout=60s"]
    with tempfile.TemporaryFile() as out:  # a large cluster's JSON goes to disk first, so its size can be checked before it is read
        try:
            res = subprocess.run(cmd, stdout=out, stderr=subprocess.PIPE, timeout=TIMEOUT_S, shell=False, check=False)
        except subprocess.TimeoutExpired as e:
            raise KubeError(f"kubectl get timed out after {TIMEOUT_S}s for context {short(ctx)}") from e
        err = res.stderr.decode("utf-8", "replace") if isinstance(res.stderr, bytes) else str(res.stderr or "")
        if res.returncode != 0:
            raise KubeError(f"kubectl get failed for context {short(ctx)}: {mask_text(err, 200)}")
        size = out.seek(0, 2)
        if size > MAX_STDOUT:
            raise KubeError(f"kubectl returned more than {MAX_STDOUT // 1_000_000} MB for context {short(ctx)}; skipped")
        out.seek(0)
        raw = out.read()
    try:
        body = json.loads(raw)
    except ValueError as e:
        raise KubeError(f"kubectl returned something other than JSON for context {short(ctx)}") from e
    return body if isinstance(body, dict) else {}


def _when(e: dict) -> datetime | None:
    meta: dict[str, Any] = e["metadata"] if isinstance(e.get("metadata"), dict) else {}
    for v in (e.get("lastTimestamp"), e.get("eventTime"), e.get("firstTimestamp"), meta.get("creationTimestamp")):
        if isinstance(v, str) and v:
            try:
                t = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                continue
            return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return None


def _workload(rows: dict[tuple[str, str], dict], ns: str, kind: str, name: str) -> dict | None:
    """The workload row an event is about: a pod or ReplicaSet named after its workload, or the workload itself."""
    if (ns, name) in rows and kind not in ("Pod", "ReplicaSet"):
        return rows[(ns, name)]
    hits = [w for (wns, w) in rows if wns == ns and name.startswith(w + "-")]
    return rows[(ns, max(hits, key=len))] if hits else None


def attach_events(part: dict, events: list[dict], cluster_of: dict[str, str], now: datetime) -> None:
    rows = {(x["cluster"], x["n"]): x for x in part["ecs"]}
    start = now - timedelta(hours=EVENT_HOURS)
    for e in events:
        obj = e.get("involvedObject") if isinstance(e, dict) and isinstance(e.get("involvedObject"), dict) else None
        t = _when(e) if obj and e.get("type") == "Warning" else None
        if not obj or not t or not start <= t <= now:
            continue
        ns = cluster_of.get(str(obj.get("namespace") or ""), str(obj.get("namespace") or ""))
        row = _workload(rows, ns, str(obj.get("kind") or ""), str(obj.get("name") or ""))
        if row is None:
            continue
        desc = k8s_events.describe(str(e.get("reason") or ""), str(e.get("message") or ""))
        if desc is None:
            continue  # not an allowlisted reason: event text can carry probe bodies and secrets
        count = e.get("count") if isinstance(e.get("count"), int) and e["count"] > 1 else None
        row.setdefault("deploys", []).append({"kind": "event", "at": iso(t), "msg": mask_text(desc + (f" (x{count})" if count else ""))})
    for x in part["ecs"]:
        x["deploys"] = sorted(x.get("deploys") or [], key=lambda d: d["at"], reverse=True)[:MAX_EVENTS]
        x.setdefault("steadyEvents", 0)


def add_to(out: dict, s: Any, ctxs: list[str], safe: Any) -> None:
    """EKS clusters into out["eks"]; with contexts named, their workloads and services join the ECS and load balancer rows."""
    out["eks"] = safe("eks", collect_eks, s) or []
    if ctxs:
        for k, v in (safe("kubernetes", collect_kube, ctxs) or {}).items():
            out.setdefault(k, []).extend(v)


def collect_kube(ctxs: list[str], now: datetime | None = None) -> dict:
    """Workloads, services and warning events of each context, as aws.json rows. With several contexts each namespace
    is shown as <context>:<namespace>."""
    now = now or datetime.now(timezone.utc)
    out: dict[str, list] = {"ecs": [], "lb": [], "rds": [], "cache": [], "search": []}
    for ctx in ctxs:
        items = [o for o in kubectl(ctx, "get", RESOURCES, "-A", "-o", "json").get("items") or [] if isinstance(o, dict)]
        events = [o for o in kubectl(ctx, "get", "events", "-A", "-o", "json").get("items") or [] if isinstance(o, dict)]
        tag = f"{short(ctx)}:" if len(ctxs) > 1 else ""
        names = {k8s_map.ns_of(o) for o in items} | {str(e["involvedObject"].get("namespace") or "") for e in events if isinstance(e.get("involvedObject"), dict)}
        cluster_of = {ns: f"{tag}{ns or 'k8s'}" for ns in names}
        for o in items:
            meta: dict[str, Any] = o["metadata"] if isinstance(o.get("metadata"), dict) else {}
            o["metadata"] = {**meta, "namespace": cluster_of.get(str(meta.get("namespace") or ""), "k8s")}
        part = k8s_map.map_objects([(o, f"kubectl {short(ctx)}") for o in items], live=True)
        attach_events(part, events, cluster_of, now)
        for k, v in part.items():
            out[k].extend(v)
    for d in out["search"]:
        d.update(type="", count=0, cpu=0, freeGB=0)
    return mask_numbers(out)
