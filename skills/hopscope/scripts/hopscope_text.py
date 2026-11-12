"""Readable text for `hopscope.py impact` and `hopscope.py query`, from hopscope_query's answers."""
from __future__ import annotations

from pathlib import Path

import hopscope_query as q


def data_dir(work: Path) -> Path:
    for sub in ("data", "site"):
        if (work / sub / "code.json").is_file():
            return work / sub
    raise q.NoData(f"no Hopscope data under {work}: run hopscope.py design or all first")


def compute_text(c: object) -> str:
    """A compute name; an unconfirmed link says so: 'x (unconfirmed link, 0.5 — confirm with hopscope.py review)'."""
    if isinstance(c, dict):
        name = q.compute_name(c)
        return f"{name} (unconfirmed link, {c.get('confidence')} — {q.REVIEW_HINT})" if c.get("unconfirmed") else name
    return str(c)


def _computes(items: list) -> str:
    return ", ".join(compute_text(c) for c in items) or "no compute linked"


def _basis(x: dict) -> str:
    return "  (service publishes to)" if x.get("basis") == "service publishes to" else ""


def impact_text(r: dict) -> str:
    if "error" in r:
        return r["error"]
    lines = [f"Changing {', '.join(r['matched'])}", f"  {r['summary']}", ""]
    if r["importers"]:
        lines.append("Files that depend on it:")
        lines += [f"  {'  ' * (x['depth'] - 1)}{x['file']}  ({x['via']}, {x['depth']} hop{'s' if x['depth'] > 1 else ''})" for x in r["importers"]]
        lines.append("")
    lines.append("Routes it can break (nearest first):" if r["routes"] else "No routes reach it.")
    lines += [f"  {x['route']:<40} {'direct' if x['distance'] == 0 else str(x['distance']) + ' hop(s)':<9} {x['reason']}" for x in r["routes"]]
    if r["services"]:
        lines += ["", "Runs on:"] + [f"  {s['service']}: {_computes(s['compute'])}" for s in r["services"]]
    if r["queues"]:
        lines += ["", "Queues and workers downstream:"]
        lines += [f"  {x['queue']} -> {x['worker'] or 'no worker found'}{' (' + x['job'] + ')' if x.get('job') else ''}{_basis(x)}" for x in r["queues"]]
    return "\n".join(lines)


def trace_text(t: dict) -> str:
    if "error" in t:
        sug = "\n".join(f"  {s['route']}" + (f"  (from {s['from']})" if s.get("from") else "") for s in t.get("suggestions", []))
        return t["error"] + (("; pick one:\n" if t.get("ambiguous") else "\nDid you mean:\n") + sug if sug else "")
    lines = [f"{t['route']}" + (f"  ({t['title']})" if t["title"] else ""), *([f"  ({t['matched']})"] if t.get("matched") else []),
             f"  handler {t['handler']}  in {t['service'] or '?'}"]
    if t.get("calledFrom"):
        more = len(t["calledFrom"]) - 3
        lines.append("  called from: " + ", ".join(t["calledFrom"][:3]) + (f" and {more} more" if more > 0 else ""))
    for e in t["entry"]:
        lines.append(f"  entry: {e['lb']} when {e['when']} -> {e['target']}")
    lines.append(f"  runs on: {_computes(t['compute'])}")
    lines.append("")
    if t["steps"]:
        for s in t["steps"]:
            cond = {"conditional": f"  [only if {s['when']}]" if s["when"] else "  [conditional]", "on error": "  [on error]"}.get(s["runs"], "")
            lines.append(f"  {'  ' * s['depth']}{s['label']:<34} {s['at']}{cond}")
    else:
        lines.append("  no code path traced (call sequences are traced for Python, Java, Kotlin and C# handlers)")
    if t["database"]:
        lines += ["", f"  database: {t['database']}"]
    for x in t["queues"]:
        lines.append(f"  queue: {x['queue']} -> {x['worker'] or 'no worker found'}{' (' + x['job'] + ')' if x.get('job') else ''}{_basis(x)}")
    return "\n".join(lines)
