"""What changed between two builds' inventories: every resource keyed by kind, scope and plain name, and the fields worth
comparing. Standard library only."""
from __future__ import annotations

from typing import Any

from scopes import plain

MAX_ITEMS = 200  # per list in a diff; a whole new account would otherwise flood changes.json
FIELDS = {  # what is compared for each kind of resource (metrics are left out: they change every run)
    "ecs": ("desired", "running", "size", "images", "taskDef", "tgs"),
    "lb": ("scheme", "rules"),
    "sqs": ("dlq", "fifo", "redrive"),
    "rds": ("engine", "class", "minAcu", "maxAcu"),
    "lambda": ("mem",),
    "ec2": ("type", "state"),
    "cache": ("type", "engine"),
    "search": ("type", "count"),
    "sfn": ("type",),
    "kinesis": ("shards", "mode"),
    "msk": ("brokers", "instance", "version"),
    "eks": ("version",),
}
NAMED = ("sns", "cognito", "apigw", "cdn", "dynamodb", "eventbridge", "ses")

def _val(v: Any) -> Any:
    return ", ".join(map(str, v)) if isinstance(v, list) else v


def _key(kind: str, name: str, row: dict) -> str:
    """A resource's identity across builds: its kind, account, region and name without a scope prefix, so a region that
    is skipped, or starts sharing a name with another, changes only its own rows. Single-scope rows keep their old keys."""
    scope = [str(row.get(k)) for k in ("account", "region") if isinstance(row, dict) and row.get(k)]
    return f"{kind}:{'|'.join(scope)}|{plain(name)}" if scope else f"{kind}:{name}"


def resources(aws: dict) -> dict[str, dict]:
    """Every resource as {"kind:name": {"kind", "name", field: value}} with only the fields worth comparing."""
    out: dict[str, dict] = {}
    for kind, fields in FIELDS.items():
        for x in aws.get(kind) or []:
            if not isinstance(x, dict):
                continue
            name = f'{x.get("cluster")}/{x.get("n")}' if kind == "ecs" else str(x.get("n") or x.get("id") or "")
            row: dict[str, Any] = {"kind": kind, "name": name}
            for f in fields:
                if f == "rules":
                    row[f] = sorted({f'{r.get("when")} -> {plain(r.get("tg"))}' for r in x.get("routes") or [] if isinstance(r, dict)})
                elif f == "tgs" and isinstance(x.get(f), list):
                    row[f] = [plain(v) for v in x[f]]
                elif f in x and x[f] is not None:
                    row[f] = x[f]
            out[_key(kind, name, x)] = row
    where: dict = aws["where"] if isinstance(aws.get("where"), dict) else {}
    for kind in NAMED:
        for x in aws.get(kind) or []:
            name = str(x.get("n") if isinstance(x, dict) else x)
            at = x if isinstance(x, dict) else (where.get(kind) or {}).get(name) or {}
            out[_key(kind, name, at)] = {"kind": kind, "name": name}
    return out


def diff_resources(old: dict[str, dict], new: dict[str, dict]) -> dict:
    added = [{"kind": new[k]["kind"], "name": new[k]["name"]} for k in sorted(new.keys() - old.keys())]
    removed = [{"kind": old[k]["kind"], "name": old[k]["name"]} for k in sorted(old.keys() - new.keys())]
    changed = []
    for k in sorted(old.keys() & new.keys()):
        a, b = old[k], new[k]
        for f in FIELDS.get(a["kind"], ()):
            if a.get(f) == b.get(f):
                continue
            if f == "rules":
                gone, came = sorted(set(a.get(f, [])) - set(b.get(f, []))), sorted(set(b.get(f, [])) - set(a.get(f, [])))
                changed.append({"kind": a["kind"], "name": a["name"], "field": f, "from": "; ".join("- " + r for r in gone), "to": "; ".join("+ " + r for r in came)})
            else:
                changed.append({"kind": a["kind"], "name": a["name"], "field": f, "from": _val(a.get(f)), "to": _val(b.get(f))})
    over = max(0, len(added) - MAX_ITEMS) + max(0, len(removed) - MAX_ITEMS) + max(0, len(changed) - MAX_ITEMS)
    return {"added": added[:MAX_ITEMS], "removed": removed[:MAX_ITEMS], "changed": changed[:MAX_ITEMS], "truncated": over}
