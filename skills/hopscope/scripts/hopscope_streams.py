"""Streams, workflows and topics a route reaches, for hopscope_query.trace: the Kinesis streams, Step Functions state
machines and Kafka topics its steps publish to (from links.json "streams"), the Lambda functions consuming each (Lambda
event source mappings, links.json "consumers") and, for a state machine, the services its definition calls (aws.json).
Standard library only; every value is treated as untrusted text and capped."""
from __future__ import annotations

from typing import Any

MAX_STR, MAX_ITEMS = 300, 50
STEP_TO_KIND = {"stream": "kinesis", "workflow": "sfn", "kafka": "kafka"}


def _clip(s: Any) -> str:
    return str(s if s is not None else "")[:MAX_STR]


def _links(data: dict, key: str) -> dict:
    m = data.get("model", {}).get(key)
    v = m if isinstance(m, dict) and m else data.get("links", {}).get(key)
    return v if isinstance(v, dict) else {}


def consumers(data: dict, kind: str, name: str) -> list[str]:
    found = _links(data, "consumers").get(f"{kind}:{name}") or []
    return [_clip(x) for x in found if isinstance(x, str)][:MAX_ITEMS]


def _calls(data: dict, name: str) -> list[str]:
    for m in data.get("aws", {}).get("sfn") or []:
        if isinstance(m, dict) and m.get("n") == name:
            return [_clip(f'{t.get("kind")} {t.get("n")}') for t in m.get("targets") or [] if isinstance(t, dict)][:MAX_ITEMS]
    return []


def reached(data: dict, service: str | None, steps: list[list]) -> list[dict]:
    """Streams, workflows and topics the route's own steps publish to. A step that names one counts exactly
    ("basis": "step"); otherwise the service's ones of that kind stand in ("service publishes to")."""
    own = [str(x) for x in _links(data, "streams").get(service or "", []) if isinstance(x, str) and ":" in x]
    out: list[dict] = []
    for kind in dict.fromkeys(STEP_TO_KIND[s[1]] for s in steps if len(s) > 2 and s[1] in STEP_TO_KIND):
        labels = " ".join(str(s[2]) for s in steps if len(s) > 2 and STEP_TO_KIND.get(s[1]) == kind)
        pool = [x.split(":", 1)[1] for x in own if x.split(":", 1)[0] == kind]
        named = [n for n in pool if n in labels]
        picks = [(n, "step") for n in named] or [(n, "service publishes to") for n in pool]
        for name, basis in picks:
            row: dict[str, Any] = {"kind": kind, "name": _clip(name), "consumers": consumers(data, kind, name)}
            if kind == "sfn":
                row["calls"] = _calls(data, name)
            row["basis"] = basis
            out.append(row)
    return out[:MAX_ITEMS]


def text(rows: list[dict]) -> list[str]:
    lines = []
    for r in rows:
        basis = "  (service publishes to)" if r.get("basis") == "service publishes to" else ""
        if r["kind"] == "sfn":
            calls = ", ".join(r.get("calls") or []) or "no integrations read"
            lines.append(f"  workflow: {r['name']} calls {calls}{basis}")
        else:
            lines.append(f"  stream: {r['kind']} {r['name']} -> {', '.join(r['consumers']) or 'no Lambda consumer found'}{basis}")
    return lines
