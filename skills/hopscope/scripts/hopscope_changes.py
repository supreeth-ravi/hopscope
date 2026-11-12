"""Answers about what changed and about drift, from a built Hopscope workdir (changes.json, drift.json, aws.json).

Pure functions over hopscope_query.load()'s data, like hopscope_query: no AWS calls, no network, no writes. Strings
are capped at hopscope_query.MAX_STR characters and lists at MAX_ITEMS so an agent's context can't be flooded.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import history
import hopscope_query as q


def recent_deploys(aws: dict, limit: int = 20) -> list[dict]:
    """Deployments and task-definition revisions across ECS services, newest first (live mode only)."""
    out = []
    for x in aws.get("ecs") or []:
        if not isinstance(x, dict):
            continue
        for d in x.get("deploys") or []:
            if isinstance(d, dict) and d.get("kind") in ("deployment", "revision") and d.get("at"):
                out.append({"at": q.clip(d["at"]), "service": q.clip(f'{x.get("cluster")}/{x.get("n")}'), "kind": q.clip(d["kind"]),
                            "taskDef": q.clip(d.get("taskDef")), "status": q.clip(d.get("rollout") or d.get("status") or "")})
    out.sort(key=lambda d: d["at"], reverse=True)
    return out[:max(1, min(limit, q.MAX_ITEMS))]


def changes(data: dict, since: str | None = None, work: Path | None = None) -> dict:
    """What changed since the previous snapshot (or a named one), recent history, and recent deploys."""
    if since:
        if work is None:
            return {"error": "since needs the workdir's snapshots"}
        try:
            d: dict[str, Any] = history.since(work, q.clip(since))
        except history.NoSnapshot as e:
            return {"error": q.clip(str(e))}
        d["history"] = []
    else:
        d = data.get("changes") or {}
        if not d:
            return {"error": "no changes.json yet: run hopscope.py all, collect or design (every run snapshots the build)",
                    "deploys": recent_deploys(data.get("aws") or {})}
    return {"snapshot": q.clip(d.get("snapshot")), "previous": q.clip(d.get("previous")) or None, "at": q.clip(d.get("at")),
            "since": q.clip(d.get("since")) or None, "summary": q.clip(d.get("summary")),
            "lines": [q.clip(x) for x in history.lines(d)[:q.MAX_ITEMS]],
            "truncated": int(d.get("truncated") or 0) + max(0, len(history.lines(d)) - q.MAX_ITEMS),
            "history": [{"snapshot": q.clip(h.get("snapshot")), "at": q.clip(h.get("at")), "summary": q.clip(h.get("summary"))}
                        for h in (d.get("history") or [])[:10] if isinstance(h, dict)],
            "deploys": recent_deploys(data.get("aws") or {})}


def _link(f: dict) -> dict:
    """The code-to-compute link confidence a finding rests on, when it rests on one."""
    conf = q.as_confidence(f.get("linkConfidence"))
    if conf is None:
        return {}
    return {"linkConfidence": conf, **({"unconfirmed": True} if q.unconfirmed(conf) else {})}


def drift(data: dict, kind: str | None = None) -> dict:
    """The drift report's findings (optionally one kind), with evidence and confidence."""
    rep = data.get("drift") or {}
    if not rep:
        return {"error": "no drift.json yet: run hopscope.py all, collect or design"}
    found = [f for f in rep.get("findings") or [] if isinstance(f, dict) and (not kind or f.get("kind") == kind)]
    return {"mode": q.clip(rep.get("mode")), "summary": q.clip(rep.get("summary")), "kind": q.clip(kind) or None,
            "checked": [q.clip(x) for x in (rep.get("checked") or [])[:10]], "skipped": [q.clip(x) for x in (rep.get("skipped") or [])[:10]],
            "total": len(found),
            "findings": [{"kind": q.clip(f.get("kind")), "severity": q.clip(f.get("severity")), "title": q.clip(f.get("title")),
                          "detail": q.clip(f.get("detail")), "confidence": q.clip(f.get("confidence")), "resource": q.clip(f.get("resource")),
                          "count": int(f.get("count") or 1) if str(f.get("count") or "1").isdigit() else 1,
                          "names": [q.clip(x) for x in (f.get("names") or [])[:5]],
                          "evidence": [q.clip(e) for e in (f.get("evidence") or [])[:8]], **_link(f)} for f in found[:q.MAX_ITEMS]],
            "uncertain": [{k: q.clip(v) for k, v in u.items()} for u in (rep.get("uncertain") or [])[:20] if isinstance(u, dict)]}


def changes_text(r: dict) -> str:
    if "error" in r:
        return r["error"]
    out = [f'Changes {r["previous"] + " → " if r["previous"] else ""}{r["snapshot"]}: {r["summary"]}']
    out += [f"  {x}" for x in r["lines"]]
    if r["truncated"]:
        out.append(f'  … {r["truncated"]} more')
    if len(r["history"]) > 1:
        out += ["", "Earlier snapshots:"] + [f'  {h["snapshot"]}  {h["summary"]}' for h in r["history"][1:]]
    if r["deploys"]:
        out += ["", "Recent deploys (ECS):"] + [f'  {d["at"]}  {d["service"]}  {d["kind"]} {d["taskDef"]}{"  " + d["status"] if d["status"] else ""}' for d in r["deploys"]]
    return "\n".join(out)


def drift_text(r: dict) -> str:
    if "error" in r:
        return r["error"]
    out = [f'Drift ({r["mode"]}): {r["summary"]}', f'  checked: {"; ".join(r["checked"])}']
    if r["skipped"]:
        out.append(f'  skipped: {"; ".join(r["skipped"])}')
    for f in r["findings"]:
        out += ["", f'{"?" if f["confidence"] == "low" else "!"} {f["title"]}', f'  {f["detail"]}']
        if f.get("unconfirmed"):
            out.append(f'  rests on an unconfirmed link, {f["linkConfidence"]} — {q.REVIEW_HINT}')
        out += [f"    {e}" for e in f["evidence"]]
    if r["uncertain"]:
        out += ["", "Unsure matches (not counted as drift):"]
        out += [f'  {u.get("kind")}: {u.get("aws") or u.get("code") or ""} ~ {u.get("files") or u.get("target") or ""}' for u in r["uncertain"]]
    return "\n".join(out)
