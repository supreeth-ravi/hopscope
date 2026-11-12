"""Snapshots of every build and what changed since the one before. Writes <workdir>/snapshots/<UTC time>/ and
<workdir>/data/changes.json.

    python history.py --data .hopscope/data --workdir .hopscope [--keep 30]

A snapshot holds copies of aws.json, links.json and model.json; code.json and flows.json can be large, so only
their hashes and counts are kept (meta.json). Each snapshot also stores diff.json, its difference from the
snapshot before it, so changes.json can list recent history without re-reading old inventories. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from iac_common import mask_numbers

KEPT = ("aws.json", "links.json", "model.json")
SUMMARISED = ("code.json", "flows.json")
MAX_ITEMS = 200  # per list in a diff; a whole new account would otherwise flood changes.json
HISTORY = 10  # recent diffs carried in changes.json
NAME = re.compile(r"^\d{8}T\d{6}Z(-\d+)?$")
FIELDS = {  # what is compared for each kind of resource (metrics are left out: they change every run)
    "ecs": ("desired", "running", "size", "images", "taskDef", "tgs"),
    "lb": ("scheme", "rules"),
    "sqs": ("dlq", "fifo", "redrive"),
    "rds": ("engine", "class", "minAcu", "maxAcu"),
    "lambda": ("mem",),
    "ec2": ("type", "state"),
    "cache": ("type", "engine"),
    "search": ("type", "count"),
}
NAMED = ("sns", "cognito", "apigw", "cdn", "dynamodb", "eventbridge", "ses")
LABELS = {"taskDef": "task definition", "tgs": "target groups", "minAcu": "min ACU", "maxAcu": "max ACU", "mem": "memory MB"}


class NoSnapshot(Exception):
    """A snapshot asked for by name does not exist."""


def _read(p: Path, default: Any = None) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def parse_time(text: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def stamp(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def file_summary(p: Path) -> dict:
    raw = p.read_bytes()
    doc = _read(p, {})
    # Hash the content without the build time, so an unchanged repo reads as unchanged.
    stable = json.dumps({k: v for k, v in doc.items() if k != "generatedAt"}, sort_keys=True).encode() if isinstance(doc, dict) else raw
    out: dict[str, Any] = {"sha256": hashlib.sha256(stable).hexdigest(), "bytes": len(raw)}
    if p.name == "code.json" and isinstance(doc, dict):
        out.update(files=len(doc.get("files") or []), routes=len(doc.get("routes") or []), services=len(doc.get("services") or []))
    elif p.name == "flows.json" and isinstance(doc, dict):
        out["routes"] = len(doc)
    return out


def snapshots(work: Path) -> list[Path]:
    root = work / "snapshots"
    return sorted((d for d in root.iterdir() if d.is_dir() and NAME.match(d.name)), key=lambda d: (d.name[:16], int((d.name + "-1").split("-")[1]))) if root.is_dir() else []


def _time_of(snap: Path) -> datetime | None:
    meta = _read(snap / "meta.json", {}) or {}
    t = parse_time(meta.get("at")) if isinstance(meta, dict) else None
    if t is None:
        try:
            t = datetime.strptime(snap.name[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return t


def take(data: Path, work: Path, at: datetime | None = None) -> Path:
    """Copy this build's key files into a new snapshot folder named by its UTC time. Snapshots only move forward: a
    build whose collection time is not after the latest snapshot's is stamped one second after it."""
    aws = _read(data / "aws.json", {}) or {}
    when = at or parse_time(aws.get("collectedAt")) or datetime.now(timezone.utc)
    prev = snapshots(work)
    last = _time_of(prev[-1]) if prev else None
    if last and when <= last:
        when = last + timedelta(seconds=1)
    root = work / "snapshots"
    root.mkdir(parents=True, exist_ok=True)
    name, n = stamp(when), 1
    while (root / name).exists():
        n += 1
        name = f"{stamp(when)}-{n}"
    snap = root / name
    snap.mkdir()
    for f in KEPT:
        if (data / f).is_file():
            shutil.copy(data / f, snap / f)
    meta = {"at": when.astimezone(timezone.utc).isoformat(timespec="seconds"), "mode": aws.get("mode", "live"),
            "files": {f: file_summary(data / f) for f in SUMMARISED if (data / f).is_file()}}
    (snap / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return snap


def prune(work: Path, keep: int, protect: str | None = None) -> list[str]:
    snaps = snapshots(work)
    gone = [d for d in snaps[:max(0, len(snaps) - max(1, keep))] if d.name != protect]
    for d in gone:
        shutil.rmtree(d)
    return [d.name for d in gone]


def load(snap: Path) -> dict:
    out: dict[str, Any] = {}
    bad = []
    for f in (*KEPT, "meta.json"):
        doc = _read(snap / f, None)
        if not isinstance(doc, dict):
            bad.append(f)
        out[f[:-5]] = doc if isinstance(doc, dict) else {}
    out["name"] = snap.name
    out["unreadable"] = [f for f in bad if f in KEPT]  # a build may lack links.json only if it never had one; treat it as unreadable all the same
    return out


# ---- what changed ---------------------------------------------------------------------------------

def _val(v: Any) -> Any:
    return ", ".join(map(str, v)) if isinstance(v, list) else v


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
                    row[f] = sorted({f'{r.get("when")} -> {r.get("tg")}' for r in x.get("routes") or [] if isinstance(r, dict)})
                elif f in x and x[f] is not None:
                    row[f] = x[f]
            out[f"{kind}:{name}"] = row
    for kind in NAMED:
        for x in aws.get(kind) or []:
            name = str(x.get("n") if isinstance(x, dict) else x)
            out[f"{kind}:{name}"] = {"kind": kind, "name": name}
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


def link_changes(old: dict, new: dict) -> list[dict]:
    a, b = old.get("placement") or {}, new.get("placement") or {}
    out = []
    for k in sorted(a.keys() | b.keys()):
        x, y = a.get(k) or {}, b.get(k) or {}
        if x.get("code") != y.get("code") or abs((x.get("confidence") or 0) - (y.get("confidence") or 0)) >= 0.1:
            out.append({"service": k, "from": x.get("code"), "to": y.get("code"), "fromConfidence": x.get("confidence"), "toConfidence": y.get("confidence")})
    return out[:MAX_ITEMS]


def _action_key(title: str) -> str:
    return re.sub(r"\d[\d,.]*", "#", title)


def action_changes(old: dict, new: dict) -> dict:
    """Action-queue items that appeared or went away. Numbers in titles change every run, so they are ignored when matching."""
    a = {_action_key(str(x[1])): str(x[1]) for x in old.get("actions") or [] if len(x) > 1}
    b = {_action_key(str(x[1])): str(x[1]) for x in new.get("actions") or [] if len(x) > 1}
    return {"new": [b[k] for k in b if k not in a][:MAX_ITEMS], "resolved": [a[k] for k in a if k not in b][:MAX_ITEMS]}


def code_changes(old_meta: dict, new_meta: dict) -> dict:
    a, b = (old_meta.get("files") or {}).get("code.json") or {}, (new_meta.get("files") or {}).get("code.json") or {}
    return {"changed": bool(a) and a.get("sha256") != b.get("sha256"), "files": [a.get("files"), b.get("files")], "routes": [a.get("routes"), b.get("routes")]}


def summary(d: dict) -> str:
    parts = [f"{len(d[k])} {k}" for k in ("added", "removed", "changed") if d.get(k)]
    if d.get("links"):
        parts.append(f'{len(d["links"])} link{"s" if len(d["links"]) > 1 else ""} moved')
    acts = d.get("actions") or {}
    if acts.get("new"):
        parts.append(f'{len(acts["new"])} new action{"s" if len(acts["new"]) > 1 else ""}')
    if acts.get("resolved"):
        parts.append(f'{len(acts["resolved"])} resolved')
    if (d.get("code") or {}).get("changed"):
        parts.append("code changed")
    return ", ".join(parts) or "no changes"


def _empty(old: dict, new: dict, why: str) -> dict:
    return {"added": [], "removed": [], "changed": [], "links": [], "actions": {"new": [], "resolved": []}, "code": {"changed": False},
            "truncated": 0, "snapshot": new["name"], "previous": old["name"], "at": new["meta"].get("at"), "since": old["meta"].get("at"),
            "first": False, "summary": why, "note": why}


def diff(old: dict, new: dict) -> dict:
    """Everything that differs between two loaded snapshots. A switch between live and design mode, or an unreadable
    earlier snapshot, gives a note instead of a diff that would list every resource as added or removed."""
    if old.get("unreadable"):
        return _empty(old, new, f'previous snapshot unreadable ({", ".join(old["unreadable"])}): nothing to compare')
    ma, mb = old["meta"].get("mode", "live"), new["meta"].get("mode", "live")
    if ma != mb:
        return _empty(old, new, f"mode changed ({ma} → {mb}): nothing to compare")
    d = diff_resources(resources(old["aws"]), resources(new["aws"]))
    d.update(links=link_changes(old["links"], new["links"]), actions=action_changes(old["model"], new["model"]), code=code_changes(old["meta"], new["meta"]))
    d.update(snapshot=new["name"], previous=old["name"], at=new["meta"].get("at"), since=old["meta"].get("at"), first=False)
    d["summary"] = summary(d)
    return d


def lines(d: dict) -> list[str]:
    """One readable line per change, for the CLI, the console and agents."""
    out = [f'+ {x["kind"]} {x["name"]}' for x in d.get("added", [])] + [f'- {x["kind"]} {x["name"]}' for x in d.get("removed", [])]
    out += [f'~ {x["name"]} {LABELS.get(x["field"], x["field"])}: {x["from"] if x["from"] not in (None, "") else "none"} → {x["to"] if x["to"] not in (None, "") else "none"}'
            for x in d.get("changed", [])]
    for x in d.get("links", []):
        conf = f' (confidence {x["fromConfidence"]} → {x["toConfidence"]})' if x["from"] == x["to"] else ""
        out.append(f'~ {x["service"]} now runs {x["to"] or "no code found"} (was {x["from"] or "none"}){conf}')
    acts = d.get("actions") or {}
    out += [f"! new action: {t}" for t in acts.get("new", [])] + [f"✓ resolved: {t}" for t in acts.get("resolved", [])]
    if (d.get("code") or {}).get("changed"):
        f, r = d["code"]["files"], d["code"]["routes"]
        out.append(f"~ code map changed: {f[0]} → {f[1]} files, {r[0]} → {r[1]} routes")
    return out


def record(data: Path, work: Path, keep: int = 30, at: datetime | None = None) -> dict:
    """Snapshot this build, diff it against the previous snapshot, prune old ones, write data/changes.json."""
    before = snapshots(work)
    snap = take(data, work, at)
    new = load(snap)
    if before:
        d = diff(load(before[-1]), new)
    else:
        d = {"added": [], "removed": [], "changed": [], "links": [], "actions": {"new": [], "resolved": []}, "code": {"changed": False},
             "truncated": 0, "snapshot": snap.name, "previous": None, "at": new["meta"].get("at"), "since": None, "first": True,
             "summary": "first snapshot: nothing to compare yet"}
    (snap / "diff.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    prune(work, keep, protect=snap.name)
    hist = []
    for s in reversed(snapshots(work)[-HISTORY:]):
        sd = _read(s / "diff.json", {}) or {}
        if sd:
            hist.append({"snapshot": s.name, "at": sd.get("at"), "since": sd.get("since"), "summary": sd.get("summary", ""), "items": lines(sd)[:20]})
    d["history"] = hist
    (data / "changes.json").write_text(json.dumps(mask_numbers(d), ensure_ascii=False), encoding="utf-8")  # values come from inventories
    return d


def since(work: Path, name: str) -> dict:
    """The difference between a named snapshot and the latest one."""
    snaps = snapshots(work)
    match = [s for s in snaps if s.name == name or s.name.startswith(name)]
    if not match:
        raise NoSnapshot(f"no snapshot {name!r}; available: {', '.join(s.name for s in snaps[-10:]) or 'none (run collect or design first)'}")
    return diff(load(match[0]), load(snaps[-1]))


# ---- deploys next to errors -----------------------------------------------------------------------

def error_jump(ts: dict) -> tuple[int, float, float] | None:
    """The first hour whose 5xx count jumps well above the usual (median) level: (hour index, usual, peak of the next 6 h)."""
    s = ts.get("series") or {}
    a, b = s.get("elb5xx") or [], s.get("e5xx") or []
    n = max(len(a), len(b))
    if not n:
        return None
    err = [(a[i] if i < len(a) else 0) + (b[i] if i < len(b) else 0) for i in range(n)]
    usual = sorted(err)[n // 2]
    for i, v in enumerate(err):
        if v >= max(20, 5 * usual) and v - usual >= 20:
            return i, usual, max(err[i:i + 6])
    return None


def deploy_suspects(aws: dict, ts: dict, before_h: float = 3, after_min: float = 15) -> list[dict]:
    """ECS deployments or new task-definition revisions in the hours before errors jumped, closest first."""
    jump, start = error_jump(ts), parse_time(ts.get("start"))
    if not jump or not start:
        return []
    t = start + timedelta(hours=jump[0])
    out: dict[tuple, dict] = {}
    for x in aws.get("ecs") or []:
        for d in x.get("deploys") or [] if isinstance(x, dict) else []:
            at = parse_time(d.get("at")) if isinstance(d, dict) and d.get("kind") in ("deployment", "revision") else None
            if at and t - timedelta(hours=before_h) <= at <= t + timedelta(minutes=after_min):
                k = (x.get("cluster"), x.get("n"), d.get("taskDef"))
                if k not in out or d["kind"] == "deployment":
                    out[k] = {"service": x.get("n"), "key": f'{x.get("cluster")}/{x.get("n")}', "env": x.get("env"), "taskDef": d.get("taskDef"),
                              "kind": d["kind"], "at": at.isoformat(), "minutes": round((t - at).total_seconds() / 60),
                              "errorsAt": t.isoformat(), "usual": jump[1], "peak": jump[2]}
    return sorted(out.values(), key=lambda s: (s["env"] != "prod", abs(s["minutes"])))


def deploy_action(aws: dict, ts: dict) -> list[list]:
    """At most one action: the deploy closest before an error jump."""
    sus = deploy_suspects(aws, ts)
    if not sus:
        return []
    s = sus[0]
    at, t = parse_time(s["at"]), parse_time(s["errorsAt"])
    if not (at and t):
        return []
    times = f'{t:%a %H:%M} UTC; {s["service"]} {"deployed" if s["kind"] == "deployment" else "registered"} {s["taskDef"]} at {at:%a %H:%M}'
    when = f'{s["minutes"]} min before' if s["minutes"] >= 0 else f'{-s["minutes"]} min after the rise began'
    others = f' Also changed in that window: {", ".join(x["service"] + " " + str(x["taskDef"]) for x in sus[1:4])}.' if len(sus) > 1 else ""
    return [["warn", f'Errors jumped {s["peak"] / max(1, s["usual"]):.0f}× right after {s["service"]} changed to {s["taskDef"]}',
             f'5xx went from about {s["usual"]:.0f} to {s["peak"]:.0f} per hour from {times}, {when}.{others}',
             "Compare it with the revision before (hopscope.py changes, Timeline tab) and roll back if it is the cause."]]


FAILURES = (("no capacity to place the task", re.compile(r"was unable to place a task", re.I),
             "Check cluster capacity, subnets and the task's CPU and memory against what is free."),
            ("image could not be pulled", re.compile(r"CannotPullContainerError", re.I),
             "Check the image tag exists and the execution role can pull from the registry."),
            ("container health checks failed", re.compile(r"failed container health checks", re.I),
             "Check the container's health command and start-up time in the latest revision's logs."),
            ("load balancer health checks failed", re.compile(r"failed ELB health checks|is unhealthy", re.I),
             "Check the target group's health-check path and port against the new revision."))
MAX_FAILURE_ACTIONS = 3


def failure_actions(aws: dict, hours: int = 72) -> list[list]:
    """One action per service whose ECS events in the last `hours` say tasks could not be placed, pulled or kept healthy."""
    end = parse_time(aws.get("collectedAt")) or datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    out = []
    for x in aws.get("ecs") or []:
        if not isinstance(x, dict):
            continue
        reasons: dict[str, int] = {}
        last = ""
        for d in x.get("deploys") or []:
            t = parse_time(d.get("at")) if isinstance(d, dict) and d.get("kind") == "event" else None
            hit = next((f for f in FAILURES if f[1].search(str(d.get("msg") or ""))), None) if t and start <= t <= end else None
            if hit:
                reasons[hit[0]] = reasons.get(hit[0], 0) + 1
                last = max(last, str(d.get("at")))
        if not reasons:
            continue
        total, top = sum(reasons.values()), max(reasons, key=lambda r: reasons[r])
        fix = next(f[2] for f in FAILURES if f[0] == top)
        level = "bad" if x.get("env", "prod") == "prod" and total >= 5 else "warn"
        out.append((x.get("env", "prod") != "prod", -total, [level, f'{x.get("n")} couldn\'t start tasks {total} times: {top}',
                    f'In the last {hours} h: {", ".join(f"{r} ({k})" for r, k in reasons.items())}; the latest at {last}.', fix]))
    return [a for *_, a in sorted(out, key=lambda o: (o[0], o[1]))][:MAX_FAILURE_ACTIONS]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--keep", type=int, default=30)
    a = ap.parse_args(argv)
    d = record(Path(a.data), Path(a.workdir), a.keep)
    print(f'changes since {d["previous"] or "nothing (first snapshot)"}: {d["summary"]} -> {Path(a.data) / "changes.json"}')


if __name__ == "__main__":
    main()
