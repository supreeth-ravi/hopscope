"""Export a built Hopscope workdir as diagrams: Mermaid (architecture flowchart, route sequence) and draw.io XML.

    hopscope.py export --format mermaid|drawio [--route "POST /orders"] --out FILE

The architecture reads left to right in four columns: entry load balancers, the services behind them,
data stores and queues, then workers and other services no load balancer routes to. Every label comes
from code or an account, so all of it is treated as untrusted text: control characters dropped, length
capped, Mermaid entity-escaped, and draw.io labels written as plain text (no html=1) through an XML
serialiser. Standard library only.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import hopscope_query as q

COLUMNS = ("Entry", "Services", "Data and queues", "Workers and jobs")
MAX_LABEL = 80
DB_TAGS = {"postgres", "mysql", "mongo"}
ICON = {  # draw.io AWS library (mxgraph.aws4): resource icon and its category colour
    "lb": ("elastic_load_balancing", "#8C4FFF"), "ecs": ("ecs", "#ED7100"), "ec2": ("ec2", "#ED7100"), "lambda": ("lambda", "#ED7100"),
    "rds": ("rds", "#C925D1"), "cache": ("elasticache", "#C925D1"), "sqs": ("sqs", "#E7157B"), "s3": ("s3", "#7AA116"),
    "cognito": ("cognito", "#DD344C"), "ses": ("simple_email_service", "#DD344C"),
}
_MM = {"#": "#35;", '"': "#quot;", "<": "#lt;", ">": "#gt;", ";": "#59;", "&": "#amp;", "|": "#124;", "`": "#96;", "%": "#37;"}

Key = tuple[str, str]


def clean(text: object) -> str:
    """Plain one-line text: control characters become spaces, length capped."""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text if text is not None else "")).strip()
    return s[:MAX_LABEL] + ("…" if len(s) > MAX_LABEL else "")


def mm(text: object) -> str:
    """Mermaid-safe label text: entity codes for every character Mermaid reads as syntax."""
    return re.sub(r'[#"<>;&|`%]', lambda m: _MM[m.group(0)], clean(text))


# ---- the architecture as a plain graph ------------------------------------------------------------

class Graph:
    def __init__(self) -> None:
        self.nodes: dict[Key, dict] = {}
        self.edges: list[dict] = []

    def node(self, kind: str, label: str, column: int) -> Key:
        key = (kind, clean(label))
        self.nodes.setdefault(key, {"label": key[1], "kind": kind, "column": column})
        return key

    def edge(self, a: Key, b: Key, label: str = "") -> None:
        e = {"from": a, "to": b, "label": clean(label)}
        if a != b and e not in self.edges:
            self.edges.append(e)

    def result(self) -> dict:
        ordered = sorted(self.nodes.items(), key=lambda kv: (kv[1]["column"], kv[1]["kind"], kv[1]["label"]))
        ids = {key: f"n{i}" for i, (key, _) in enumerate(ordered)}
        return {"nodes": [{"id": ids[k], **v} for k, v in ordered],
                "edges": [{"from": ids[e["from"]], "to": ids[e["to"]], "label": e["label"]} for e in self.edges]}


def _view(data: dict, key: str) -> dict:
    return data["model"].get(key) or data["links"].get(key) or {}


def architecture(data: dict) -> dict:
    """{nodes: [{id, label, kind, column}], edges: [{from, to, label}]}, the same for the same data."""
    infra = {r.get("n"): r for r in data["model"].get("infra", [])}
    by_key = {r["key"]: r for r in infra.values() if r.get("key")}
    g = Graph()
    behind_lb: dict[str, list[tuple[Key, str]]] = {}
    for lb in data["aws"].get("lb", []) or []:
        lb_key = g.node("lb", lb.get("n", "load balancer"), 0)
        for r in lb.get("routes", []):
            for k in r.get("services", []):
                row = by_key.get(k)
                if row and row.get("env", "prod") == "prod":
                    behind_lb.setdefault(row["n"], []).append((lb_key, r.get("when", "")))
    compute: dict[str, Key] = {}
    for name in sorted(behind_lb):
        compute[name] = g.node(infra.get(name, {}).get("kind", "ecs"), name, 1)
        for lb_key, when in behind_lb[name]:
            g.edge(lb_key, compute[name], when)
    service_compute: dict[str, str] = {}
    for svc, p in sorted(_view(data, "placement").items()):
        prod = [c for c in p.get("compute", []) if infra.get(c, {}).get("env", "prod") == "prod"]
        if prod:
            service_compute[svc] = prod[0]
            compute.setdefault(prod[0], g.node(infra.get(prod[0], {}).get("kind", "ecs"), prod[0], 3))
    _data_edges(data, g, service_compute, compute)
    return g.result()


def _data_edges(data: dict, g: Graph, service_compute: dict[str, str], compute: dict[str, Key]) -> None:
    """Service -> database, cache, S3, Cognito, SES (from what its files talk to), service -> queue -> worker."""
    aws, model = data["aws"], data["model"]
    tags: dict[str, set[str]] = {}
    for f in data["code"].get("files", []):
        svc = q.service_of(data, f["p"])
        if svc:
            tags.setdefault(svc, set()).update(f.get("x", []))
    rds = sorted(r["n"] for r in model.get("infra", []) if r.get("kind") == "rds" and r.get("env", "prod") == "prod")
    cache = (aws.get("cache") or [{}])[0].get("n") or "Cache"
    pool = (aws.get("cognito") or [{}])[0].get("n") or "Cognito"
    workers, queues, db_for = _view(data, "queueWorkers"), _view(data, "queues"), _view(data, "dbFor")
    kinds = {r.get("n"): r.get("kind", "ecs") for r in model.get("infra", [])}
    for svc, comp in sorted(service_compute.items()):
        me, t = compute[comp], tags.get(svc, set())
        db = db_for.get(svc) or model.get("primaryDb") or (rds[0] if rds else None)
        if t & DB_TAGS and db:
            g.edge(me, g.node("rds", db, 2), "SQL")
        for tag, kind, label, what in (("cache", "cache", cache, "cache"), ("s3", "s3", "S3", "objects"),
                                       ("cognito", "cognito", pool, "sign-in"), ("ses", "ses", "SES", "email")):
            if tag in t:
                g.edge(me, g.node(kind, label, 2), what)
        for qn in sorted(queues.get(svc, [])):
            w = workers.get(qn) or []
            if not (w and w[0] == comp):
                g.edge(me, g.node("sqs", qn, 2), "sends")
    for qn in sorted(workers):
        w = workers[qn] or []
        if w:
            worker = compute.get(w[0]) or g.node(kinds.get(w[0], "ecs"), w[0], 3)
            g.edge(g.node("sqs", qn, 2), worker, w[1] if len(w) > 1 else "")


# ---- Mermaid --------------------------------------------------------------------------------------

def mermaid_architecture(data: dict) -> str:
    g = architecture(data)
    lines = ["flowchart LR"]
    for col, title in enumerate(COLUMNS):
        members = [n for n in g["nodes"] if n["column"] == col]
        if members:
            lines.append(f'  subgraph c{col}["{mm(title)}"]')
            lines += [f'    {n["id"]}["{mm(n["label"])}"]' for n in members]
            lines.append("  end")
    for e in g["edges"]:
        lines.append(f'  {e["from"]} -->|"{mm(e["label"])}"| {e["to"]}' if e["label"] else f'  {e["from"]} --> {e["to"]}')
    return "\n".join(lines) + "\n"


def mermaid_trace(data: dict, route: str | int) -> str:
    """A sequence diagram of one route: client, load balancer, service, then each system its code path reaches."""
    t = q.trace(data, route)
    if "error" in t:
        raise ValueError(t["error"])
    names: dict[str, str] = {}
    lines = ["sequenceDiagram"]

    def who(label: str) -> str:
        if label not in names:
            names[label] = f"p{len(names)}"
            lines.insert(1 + len(names) - 1, f"  participant {names[label]} as {mm(label)}")
        return names[label]

    client = who("Client")
    lb = who(t["entry"][0]["lb"]) if t["entry"] else None
    svc = who(t["compute"][0] if t["compute"] else t["service"] or "service")
    lines.append(f"  {client}->>{lb or svc}: {mm(t['route'])}")
    if lb:
        lines.append(f"  {lb}->>{svc}: {mm(t['entry'][0]['when'])}")
    queue = t["queues"][0]["queue"] if t["queues"] else None
    for s in t["steps"]:
        system, _, api = s["label"].partition(": ")
        if s["kind"] == "call":
            target, msg = "", s["label"]
        elif s["kind"] == "sql":
            target, msg = (t["database"] or system), api or s["label"]
        elif s["kind"] == "queue" or s["label"].startswith(("SQS: SendMessage", "SNS: Publish")):
            target, msg = (queue or system), api or s["label"]
        elif s["kind"] == "http":
            target, msg = "Other service", s["label"]
        else:
            target, msg = system or s["kind"], api or s["label"]
        line = f"  {svc}->>{who(target) if target else svc}: {mm(msg)}"
        if s["runs"] != "always":
            lines += [f"  opt {mm(s['when'] or s['runs'])}", "  " + line, "  end"]
        else:
            lines.append(line)
    for x in t["queues"]:
        if x["worker"]:
            lines.append(f"  {who(x['queue'])}-->>{who(x['worker'])}: {mm(x['job'] or 'message')}")
    lines.append(f"  {svc}-->>{lb or client}: response")
    if lb:
        lines.append(f"  {lb}-->>{client}: response")
    return "\n".join(lines) + "\n"


# ---- draw.io --------------------------------------------------------------------------------------

def _style(kind: str) -> str:
    icon, colour = ICON.get(kind, ICON["ecs"])
    return (f"sketch=0;outlineConnect=0;fontColor=#232F3E;gradientColor=none;fillColor={colour};strokeColor=#ffffff;dashed=0;"
            f"verticalLabelPosition=bottom;verticalAlign=top;align=center;fontSize=12;fontStyle=0;aspect=fixed;"
            f"shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.{icon};")


def drawio_architecture(data: dict) -> str:
    """Uncompressed draw.io file: one vertex per node in four columns, one edge per link."""
    g = architecture(data)
    mxfile = ET.Element("mxfile", host="hopscope")
    diagram = ET.SubElement(mxfile, "diagram", id="architecture", name="Architecture")
    model = ET.SubElement(diagram, "mxGraphModel", grid="1", gridSize="10", page="0")
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", {"id": "0"})
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})
    rows: dict[int, int] = {}
    for n in g["nodes"]:
        row = rows.get(n["column"], 0)
        rows[n["column"]] = row + 1
        cell = ET.SubElement(root, "mxCell", {"id": n["id"], "value": n["label"], "style": _style(n["kind"]), "vertex": "1", "parent": "1"})
        ET.SubElement(cell, "mxGeometry", {"x": str(40 + 260 * n["column"]), "y": str(40 + 130 * row), "width": "60", "height": "60", "as": "geometry"})
    for i, e in enumerate(g["edges"]):
        cell = ET.SubElement(root, "mxCell", {"id": f"e{i}", "value": e["label"], "style": "edgeStyle=orthogonalEdgeStyle;rounded=1;endArrow=block;fontSize=10;",
                                              "edge": "1", "parent": "1", "source": e["from"], "target": e["to"]})
        ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
    return ET.tostring(mxfile, encoding="unicode") + "\n"
