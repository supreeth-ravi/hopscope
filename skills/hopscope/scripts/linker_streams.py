"""Which code publishes to which stream, topic or workflow, and which Lambda functions consume them.

A code folder publishes to a Kinesis stream, a Step Functions state machine or a Kafka topic (one a Lambda event source
mapping reads) when one of its files that uses that kind of client names it: as a whole quoted string, as the tail of an
ARN, or through the container setting of a service placed on that folder. Consumers come from Lambda event source
mappings (SQS, Kinesis, DynamoDB streams, MSK and self-managed Kafka). Keys are "<kind>:<name>"."""
from __future__ import annotations

import re
from pathlib import Path

from langs.common import read_contained

KIND_OF_EXTERNAL = {"kinesis": "kinesis", "sfn": "sfn", "kafka": "kafka"}
MIN_NAME = 4


def _names(inv: dict) -> dict[str, set[str]]:
    topics = {t for m in inv.get("esm") or [] if isinstance(m, dict) for t in m.get("topics") or [] if isinstance(t, str)}
    return {"kinesis": {str(k.get("n")) for k in inv.get("kinesis") or [] if isinstance(k, dict) and k.get("n")},
            "sfn": {str(m.get("n")) for m in inv.get("sfn") or [] if isinstance(m, dict) and m.get("n")},
            "kafka": topics}


def mentions(text: str, name: str) -> bool:
    """The name stands alone as a quoted string or ends an ARN/path (…:stateMachine:name, …stream/name)."""
    if len(name) < MIN_NAME:
        return False
    return re.search(rf"(['\"]){re.escape(name)}\1|[:/]{re.escape(name)}['\"]", text) is not None


def consumers_of(inv: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in inv.get("esm") or []:
        if not isinstance(m, dict) or not m.get("fn"):
            continue
        kind = "kafka" if m.get("kind") in ("msk", "kafka") else str(m.get("kind") or "")
        keys = [f"kafka:{t}" for t in m.get("topics") or []] if kind == "kafka" else [f"{kind}:{m.get('source')}"]
        for k in keys:
            if m["fn"] not in out.setdefault(k, []):
                out[k].append(str(m["fn"]))
    return out


def _service_of(path: str, services: list[str]) -> str | None:
    for s in sorted(services, key=len, reverse=True):
        if s == "." or path.startswith(s + "/"):
            return s
    return None


def stream_links(code: dict, inv: dict, services: list[str], placement: dict[str, dict], base: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """({code folder: ["kinesis:x", "sfn:y", "kafka:z"]}, {"<kind>:<name>": [consuming Lambda functions]})."""
    names = _names(inv)
    found: dict[str, set[str]] = {}
    root = base.resolve()
    for f in code.get("files") or []:
        kinds = [KIND_OF_EXTERNAL[x] for x in f.get("x") or [] if x in KIND_OF_EXTERNAL]
        svc = _service_of(str(f.get("p", "")), services) if kinds else None
        if not svc:
            continue
        text = read_contained(root, str(f["p"]))
        for kind in kinds:
            for n in names[kind] if text else ():
                if mentions(text or "", n):
                    found.setdefault(svc, set()).add(f"{kind}:{n}")
    for x in inv.get("ecs") or []:  # a stream or state machine named in the container settings of a placed service
        p = placement.get(f'{x.get("cluster")}/{x.get("n")}')
        if not p:
            continue
        for r in x.get("refs") or []:
            for kind, pool in names.items():
                v = str(r.get("v") or "")
                hit = v if v in pool else next((n for n in pool if v.endswith((f":{n}", f"/{n}"))), None)
                if hit:
                    found.setdefault(p["code"], set()).add(f"{kind}:{hit}")
    return {k: sorted(v) for k, v in sorted(found.items())}, consumers_of(inv)
