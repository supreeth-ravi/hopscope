"""Drift, code side: queues and container settings in AWS (or, in design mode, the files) against the code.

Used by drift.py. Reads the scanned code files' text (tests, fixtures and docs are skipped) and finds queue names
written in it. Standard library only.
"""
from __future__ import annotations

import re
from pathlib import Path

import drift_names as n

STRONG_QUEUE = re.compile(r"""["']([A-Za-z0-9_-]{3,80}\.fifo)["']|https://sqs\.[a-z0-9-]+\.amazonaws\.com/[0-9*]{12}/([A-Za-z0-9_.-]{1,80})""")
VAR_QUEUE = re.compile(r"""\b[A-Z][A-Z0-9_]*QUEUE[A-Z0-9_]*\s*[:=]\s*["']([A-Za-z0-9_.-]{3,80})["']""")
NOT_APP_CODE = re.compile(r"(^|/)(tests?|__tests__|spec|specs|fixtures?|testdata|docs?|examples?)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[jt]sx?$|\.(md|rst|txt)$", re.I)
MAX_FILE = 300_000


def texts(code: dict) -> list[tuple[str, str]]:
    """(path, text) of the scanned application files; tests, fixtures and docs name queues that need not exist."""
    try:
        base = Path(code.get("base") or ".").resolve()
    except (OSError, RuntimeError):
        return []
    out = []
    for f in code.get("files", []):
        rel = str(f.get("p", "")) if isinstance(f, dict) else ""
        if not rel or NOT_APP_CODE.search(rel):
            continue
        p = base / rel
        try:
            # code.json is data: read only regular files that stay under the scanned base, never through a symlink
            if p.is_symlink() or not p.resolve().is_relative_to(base):
                continue
            if p.is_file() and p.stat().st_size <= MAX_FILE:
                out.append((rel, p.read_text(errors="ignore")))
        except (OSError, RuntimeError, ValueError):
            continue
    return out


def queue_literals(files: list[tuple[str, str]]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Queue names written in the code. Strong: quoted .fifo names and SQS URLs (certainly SQS). Weak: values of variables
    named like ORDERS_QUEUE, which are as often Celery, Redis or RabbitMQ queue names."""
    strong: dict[str, list[str]] = {}
    weak: dict[str, list[str]] = {}
    for path, text in files:
        for i, line in enumerate(text.splitlines(), 1):
            for m in STRONG_QUEUE.finditer(line):
                strong.setdefault(next(g for g in m.groups() if g), []).append(f"{path}:{i}")
            for m in VAR_QUEUE.finditer(line):
                weak.setdefault(m.group(1), []).append(f"{path}:{i}")
    return strong, weak


def family_prefix(fam: str) -> str:
    """The static start of a family name (tenant_events_*.fifo -> tenant_events_), or "" when too short to search for."""
    head = fam.split("*", 1)[0] if "*" in fam else ""
    return head if len(head.strip("-_.")) >= 4 else ""


def referenced(queues: list[dict], files: list[tuple[str, str]], literals: dict[str, list[str]], extra: set[str]) -> dict[str, str]:
    """Queue name -> where the code names it: the name (or its environment-free form) standing alone in a file, a queue
    name written in the code that matches it, a family's static prefix (per-tenant queues are named at run time), or a
    link or container setting that already ties it to code."""
    names = [str(q.get("n")) for q in queues]
    variants: dict[str, set[str]] = {}
    for q in names:
        for v in {re.sub(r"\.fifo$", "", q), "-".join(n.canon(q))}:
            if len(v) >= 4:
                variants.setdefault(v, set()).add(q)
    found: dict[str, str] = {q: "linked to code" for q in names if q in extra}
    if variants:
        rx = re.compile(r"(?<![\w-])(" + "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True)) + r")(?:\.fifo)?(?![\w-])")
        for path, text in files:
            for m in rx.finditer(text):
                for q in variants[m.group(1)]:
                    found.setdefault(q, f"{path}:{text.count(chr(10), 0, m.start()) + 1}")
    for lit, where in literals.items():
        hit, level = n.match(lit, names)
        if hit and level != "low":
            found.setdefault(hit, where[0])
    prefixes = {family_prefix(str(q.get("fam") or "")) for q in queues} - {""}
    for pre in prefixes:
        named_in = next((path for path, text in files if pre in text), None)
        if named_in:
            for row in queues:
                if str(row.get("fam") or "").startswith(pre):
                    found.setdefault(str(row.get("n")), f"{named_in} (names {pre}…)")
    return found


def ref_queue(r: dict) -> str | None:
    v = str(r.get("v", ""))
    if "sqs." in v and "/" in v:
        return v.rsplit("/", 1)[1]
    if "QUEUE" in str(r.get("k", "")).upper() and re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", v):
        return v
    return None


LIVE_MISSING = "The service is configured for a resource that isn't there: calls to it fail."
DESIGN_MISSING = "It isn't defined in these files; it may be defined in another repo or created outside infrastructure files."
DESIGN_QUEUE_MISSING = "It isn't defined in these files; it may be defined in another repo or created at runtime."


def named_in(name: str, files: list[tuple[str, str]]) -> str | None:
    """Where a scanned file names `name` standing alone (path:line), or None."""
    stem = re.sub(r"\.fifo$", "", name)
    if len(stem) < 3:
        return None
    rx = re.compile(r"(?<![\w-])" + re.escape(stem) + r"(?:\.fifo)?(?![\w-])")
    for path, text in files:
        m = rx.search(text)
        if m:
            return f"{path}:{text.count(chr(10), 0, m.start()) + 1}"
    return None


def missing_ref(key: str, r: dict, missing: str, where: str, files: list[tuple[str, str]], design: bool) -> dict:
    """A setting that points at a resource the target lacks. In design mode the resource may be defined in another repo:
    when a scanned file names it, the finding is informational only."""
    v = str(r.get("v", ""))
    evidence = [f'setting: {r.get("k")} = {v}', f"{where}: no {missing.split()[0]} named like it"]
    if not design:
        return n.finding("ref_missing", f'{key}: {r.get("k")} points at {missing}, which does not exist in {where}', LIVE_MISSING, evidence, key, "ecs")
    title = f'{key}: {r.get("k")} points at {missing}, which is not defined in these files'
    seen = named_in(missing.split(" ", 1)[1], files)
    if seen:
        return n.finding("ref_missing", title, DESIGN_MISSING, evidence + [f"code: named in {seen}"], key, "ecs", confidence="low", severity="info")
    return n.finding("ref_missing", title, DESIGN_MISSING, evidence + ["code: not named in any scanned file"], key, "ecs")


def check(code: dict, links: dict, target: dict, where: str, files: list[tuple[str, str]], similar: bool,
          design: bool = False) -> tuple[list[dict], list[dict]]:
    """Unreferenced queues (grouped), queues the code names that don't exist, and settings pointing at missing resources."""
    findings: list[dict] = []
    uncertain: list[dict] = []
    sqs = [q for q in target.get("sqs") or [] if isinstance(q, dict) and q.get("n")]
    queues = [str(q["n"]) for q in sqs]
    strong, weak = queue_literals(files)
    linked = {q for qs in (links.get("queues") or {}).values() for q in qs}
    ecs = [x for x in target.get("ecs") or [] if isinstance(x, dict)]
    from_env = {q for x in ecs for r in x.get("refs") or [] if (q := ref_queue(r))}
    found = referenced(sqs, files, {**weak, **strong}, linked | from_env)
    unused = [q for q in sqs if not q.get("dlq") and str(q["n"]) not in found]
    for g in n.group("sqs", unused):
        findings.append(n.grouped_finding("queue_unreferenced", g, f"in {where}, no code references it", "sqs",
                                          "Left over from removed code, used by something outside these repos, or named only through configuration.",
                                          f"code: not named in {len(files)} application files, container settings or links"))
    if queues:  # with no SQS at all, queue names in the code belong to another broker
        for lit, places in sorted(strong.items()):
            hit, level = n.match(lit, queues)
            if level == "low":
                if similar:
                    uncertain.append({"kind": "sqs", "code": lit, "target": hit, "why": f"code names {lit}; {hit} in {where} is similar"})
            elif hit is None:
                evidence = [f"code: {p}" for p in places[:4]] + [f"{where}: no queue named like it among {len(queues)}"]
                if design:  # the files of these repos are not the whole system: informational only
                    findings.append(n.finding("queue_missing", f"Code references queue {lit}, which is not defined in these files", DESIGN_QUEUE_MISSING,
                                              evidence, lit, "sqs", confidence="low", severity="info"))
                else:
                    findings.append(n.finding("queue_missing", f"Code references queue {lit}, which is not in {where}",
                                              "Messages sent to it fail, or it lives in another account or region.", evidence, lit, "sqs"))
    hosts = {h.split(".")[0] for d in target.get("rds") or [] if isinstance(d, dict) for h in (d.get("endpoint"), d.get("clusterEndpoint")) if h}
    hosts |= {"-".join(n.canon(str(d.get("n")))) for d in target.get("rds") or [] if isinstance(d, dict)}
    for x in ecs:
        for r in x.get("refs") or []:
            q, v = ref_queue(r), str(r.get("v", ""))
            missing = None
            if q and (queues or "sqs." in v) and n.match(q, queues)[0] is None:
                missing = f"queue {q}"
            elif ".rds." in v:
                host = re.sub(r"^[a-z]+://", "", v).split(".")[0]
                if host not in hosts and "-".join(n.canon(host)) not in hosts:
                    missing = f"database {host}"
            if missing:
                findings.append(missing_ref(n.key("ecs", x), r, missing, where, files, design))
    return findings, uncertain


CLIENT_DETAIL = ("The frontend sends a request no mapped route answers: a path or method that changed on one side, a route the scanner "
                 "can't read, or a backend outside these repos.")


MAX_CALL_FINDINGS = 50


def unmatched_calls(code: dict) -> tuple[list[dict], list[dict]]:
    """Frontend HTTP calls (code.json clientCalls) that match no backend route, one per method and path, as
    (findings, uncertain). They are findings only when the scan has backend routes and at least one call matched a
    route; otherwise the backend is probably not among the scanned repos and they are listed as uncertain. At most 50
    findings, then one row counting the rest."""
    files = [str(f.get("p", "")) if isinstance(f, dict) else "" for f in code.get("files", [])]
    rows = [c for c in code.get("clientCalls") or [] if isinstance(c, list) and len(c) in (5, 6) and isinstance(c[0], int) and 0 <= c[0] < len(files)]
    groups: dict[str, list[str]] = {}
    for c in rows:
        if c[4] is None:
            groups.setdefault(f"{str(c[2])[:10]} {str(c[3])[:200]}", []).append(f"{files[c[0]]}:{c[1]}")
    n_routes = len(code.get("routes") or [])
    if not n_routes or not any(c[4] is not None for c in rows):
        why = "no backend routes were scanned" if not n_routes else "none of the frontend calls matched a route"
        return [], [{"kind": "route", "code": call, "target": "no route", "why": f"frontend calls {call} ({places[0]}); {why}"}
                    for call, places in sorted(groups.items())]
    out = []
    for call, places in sorted(groups.items())[:MAX_CALL_FINDINGS]:
        evidence = [f"code: {p}" for p in places[:4]] + [f"routes: none of {n_routes} mapped routes serves it"]
        out.append(n.finding("client_unmatched", f"Frontend calls {call}, which no backend route serves", CLIENT_DETAIL, evidence, call, "route",
                             names=places))
    rest = sorted(groups.items())[MAX_CALL_FINDINGS:]
    if rest:
        names = [call for call, _ in rest]
        out.append(n.finding("client_unmatched", f"{len(rest)} more frontend calls no backend route serves", CLIENT_DETAIL,
                             [f"call: {c}" for c in names[:6]], f"{len(rest)} more calls", "route", names=names))
    return out, []
