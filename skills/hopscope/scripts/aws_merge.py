"""Merge the inventories and time series of several regions and accounts into one aws.json and ts.json.

Every row gets `region` and `account` (the account's label, never its number). A name two scopes share gets a scope prefix
in both (scopes.tag), and every reference to it inside that scope follows: load balancer routes to ECS keys, target
groups, event source mappings, EventBridge targets, Step Functions targets and the time-series labels. Counts (NAT
gateways, ECR repositories, secrets, IP addresses) add up; global services (CloudFront, S3, cost) come once per account.
"""
from __future__ import annotations

import copy
from collections import Counter
from datetime import datetime
from typing import Any

from iac_common import mask_numbers
from scopes import tag

NAMED = ("ec2", "lb", "rds", "sqs", "lambda", "sns", "cache", "search", "apigw", "cognito", "sfn", "kinesis", "msk", "eks")
STRINGS = ("dynamodb", "ses", "eventbridge")
COUNTS = ("nat", "ecr", "secrets")
REF_KIND = {"lambda": "lambda", "sqs": "sqs", "sns": "sns", "sfn": "sfn", "kinesis": "kinesis", "dynamodb": "dynamodb", "msk": "msk"}
SUM_SERIES, MAX_SERIES = ("req", "elb5xx", "elb503", "e5xx"), ("p95", "p50")


def _names(inv: dict, kind: str) -> set[str]:
    if kind == "cluster":
        return {str(x.get("cluster")) for x in inv.get("ecs") or []}
    if kind == "tg":
        return {t for x in inv.get("ecs") or [] for t in x.get("tgs") or []} | {r.get("tg") for lb in inv.get("lb") or [] for r in lb.get("routes") or []} - {None}
    if kind in STRINGS:
        return {str(x) for x in inv.get(kind) or []}
    return {str(x.get("n")) for x in inv.get(kind) or [] if isinstance(x, dict)}


def renames(parts: list[dict]) -> list[dict[str, dict[str, str]]]:
    """Per part, {kind: {name: prefixed name}} for the names that collide with another part."""
    kinds = ("cluster", "tg", *NAMED, *STRINGS)
    out: list[dict[str, dict[str, str]]] = [{k: {} for k in kinds} for _ in parts]
    for kind in kinds:
        held = [_names(p["inv"], kind) for p in parts]
        counts = Counter(n for names in held for n in names)
        for name, c in counts.items():
            if c < 2:
                continue
            holders = [(p["account"], p["region"]) for p, names in zip(parts, held, strict=True) if name in names]
            for i, p in enumerate(parts):
                if name in held[i]:
                    out[i][kind][name] = f'{tag(holders, p["account"], p["region"])}:{name}'
    return out


def _apply(inv: dict, ren: dict[str, dict[str, str]]) -> None:
    """Rename one part's colliding names and every reference to them (on a copy owned by merge)."""
    r = lambda kind, n: ren.get(kind, {}).get(n, n)  # noqa: E731 - a short local lookup
    for x in inv.get("ecs") or []:
        x["cluster"] = r("cluster", x.get("cluster"))
        x["tgs"] = [r("tg", t) for t in x.get("tgs") or []]
    for kind in NAMED:
        for x in inv.get(kind) or []:
            x["n"] = r(kind, x.get("n"))
    for kind in STRINGS:
        inv[kind] = [r(kind, x) for x in inv.get(kind) or []]
    for lb in inv.get("lb") or []:
        for route in lb.get("routes") or []:
            route["tg"] = r("tg", route.get("tg"))
            route["services"] = [f'{r("cluster", k.split("/", 1)[0])}/{k.split("/", 1)[1]}' if "/" in k else k for k in route.get("services") or []]
    for m in inv.get("esm") or []:
        m["fn"] = r("lambda", m.get("fn"))
        if m.get("kind") in REF_KIND:
            m["source"] = r(REF_KIND[m["kind"]], m.get("source"))
    for t in inv.get("ebTargets") or []:
        t["rule"] = r("eventbridge", t.get("rule"))
        t["n"] = r(REF_KIND.get(t.get("kind"), ""), t.get("n"))
    for sm in inv.get("sfn") or []:
        for t in sm.get("targets") or []:
            t["n"] = r(REF_KIND.get(t.get("kind"), ""), t.get("n"))


def _rename_ts(ts: dict, ren: dict[str, dict[str, str]]) -> None:
    lab = ts.setdefault("labels", {})
    if lab.get("lb"):
        lab["lb"] = ren["lb"].get(lab["lb"], lab["lb"])
    if lab.get("db"):
        lab["db"] = ren["rds"].get(lab["db"], lab["db"])
    lab["queues"] = [ren["sqs"].get(q, q) for q in lab.get("queues") or []]
    for b in ts.get("byService") or []:
        b["tg"] = ren["tg"].get(b.get("tg"), b.get("tg"))


def _hour(start: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(start))
    except ValueError:
        return None


def _align(tss: list[dict]) -> tuple[str, list[dict]]:
    """Every series cut to start at the latest start hour, so regions collected either side of an hour boundary line up."""
    starts = [(t, _hour(t.get("start"))) for t in tss]
    known = [h for _, h in starts if h]
    if not known:
        return str(next((t.get("start") for t in tss if t.get("start")), "")), tss
    top = max(known)
    out = []
    for t, h in starts:
        skip = max(0, int((top - h).total_seconds() // 3600)) if h else 0
        out.append({**t, "series": {k: v[skip:] for k, v in (t.get("series") or {}).items() if isinstance(v, list)}} if skip else t)
    return top.isoformat(), out


def _combine(series: list[list[float]], how: Any) -> list[float]:
    n = min((len(s) for s in series), default=0)  # aligned series: only the hours every region has
    return [round(how(s[i] if i < len(s) else 0 for s in series), 4) for i in range(n)]


def _peak_share(t: dict) -> float:
    vals, lab = t["series"].get("dbcap") or [0], t["labels"]
    return max(vals) / (lab.get("dbMax") or 1) if lab.get("dbKind") == "acu" else max(vals) / 100


def merge_ts(tss: list[dict], parts: list[dict], daily: list[dict]) -> dict:
    """One ts.json: traffic and errors added up, latency the worst of any region, the most saturated database and the
    container closest to its memory limit, the three busiest queues, every target group."""
    if len(tss) == 1:
        return {**tss[0], "dailyCost": daily}
    start, tss = _align(tss)
    out: dict[str, Any] = {"start": start, "hours": 72, "series": {}, "byService": []}
    with_lb = [t for t in tss if t["labels"].get("lb")]
    for k in SUM_SERIES + MAX_SERIES:
        got = [t["series"][k] for t in with_lb if t["series"].get(k)]
        if got:
            out["series"][k] = _combine(got, sum if k in SUM_SERIES else max)
    regions = {p["region"] for p, t in zip(parts, tss, strict=True) if t["labels"].get("lb")}
    where = f'{len(regions)} regions' if len(regions) > 1 else f'{len(with_lb)} accounts'
    lb = with_lb[0]["labels"]["lb"] if len(with_lb) == 1 else (f"{len(with_lb)} load balancers in {where}" if with_lb else None)
    labels: dict[str, Any] = {"lb": lb, "db": None, "dbKind": "cpu", "dbMax": None, "mem": None, "queues": []}
    dbs = [t for t in tss if t["labels"].get("db") and t["series"].get("dbcap")]
    if dbs:
        top = max(dbs, key=_peak_share)
        labels.update(db=top["labels"]["db"], dbKind=top["labels"].get("dbKind", "cpu"), dbMax=top["labels"].get("dbMax"))
        out["series"].update({k: top["series"][k] for k in ("dbcap", "dbavg") if k in top["series"]})
    mems = [t for t in tss if t["labels"].get("mem") and t["series"].get("mem")]
    if mems:
        top = max(mems, key=lambda t: max(t["series"]["mem"] or [0]))
        labels["mem"], out["series"]["mem"] = top["labels"]["mem"], top["series"]["mem"]
    queues = [(q, t["series"].get(f"q{i}") or []) for t in tss for i, q in enumerate(t["labels"].get("queues") or [])]
    for i, (q, vals) in enumerate(sorted(queues, key=lambda qv: -sum(qv[1]))[:3]):
        labels["queues"].append(q)
        out["series"][f"q{i}"] = vals
    out["labels"] = labels
    out["byService"] = sorted((b for t in tss for b in t.get("byService") or []), key=lambda b: -b.get("req", 0))
    out["dailyCost"] = daily
    return out


def _daily(globals_: dict[str, dict]) -> list[dict]:
    days: dict[str, float] = {}
    for g in globals_.values():
        for d in g.get("dailyCost") or []:
            days[d["d"]] = round(days.get(d["d"], 0) + float(d.get("usd") or 0), 2)
    return [{"d": d, "usd": v} for d, v in sorted(days.items())]


def _cost(globals_: dict[str, dict]) -> list[list]:
    totals: dict[str, float] = {}
    for g in globals_.values():
        for name, usd in g.get("cost") or []:
            totals[name] = round(totals.get(name, 0) + float(usd), 2)
    return [[k, v] for k, v in sorted(totals.items(), key=lambda kv: -kv[1])]


def merge(parts: list[dict], globals_: dict[str, dict], skipped: list[dict], collected_at: str = "") -> tuple[dict, dict]:
    """parts: [{"account": label, "region": r, "inv": regional inventory, "ts": hourly series}], in a stable order;
    globals_: {label: {"cdn", "s3", "cost", "dailyCost"}}. Returns (aws.json, ts.json), with no account number in either."""
    parts = copy.deepcopy(parts)
    ren = renames(parts)
    aws: dict[str, Any] = {k: [] for k in ("ecs", *NAMED, *STRINGS, "ebTargets", "esm", "cdn")}
    aws.update({k: 0 for k in COUNTS}, eips={"total": 0, "unattached": 0})
    where: dict[str, dict[str, dict]] = {k: {} for k in STRINGS}  # plain-name lists can't carry fields: where each name is
    counts: list[dict] = []
    for p, rn in zip(parts, ren, strict=True):
        inv = p["inv"]
        _apply(inv, rn)
        here = {"region": p["region"], "account": p["account"]}
        for k in STRINGS:
            where[k].update({str(n): here for n in inv.get(k) or []})
        counts.append({**here, **{k: int(inv.get(k) or 0) for k in COUNTS}})
        _rename_ts(p.setdefault("ts", {"labels": {}, "series": {}}), rn)
        for k, v in inv.items():
            if isinstance(v, list):
                for row in v:
                    if isinstance(row, dict):
                        row.update(region=p["region"], account=p["account"])
                aws.setdefault(k, []).extend(v)
            elif k in COUNTS:
                aws[k] += int(v or 0)
            elif k == "eips" and isinstance(v, dict):
                aws["eips"] = {x: aws["eips"][x] + int(v.get(x) or 0) for x in ("total", "unattached")}
    for label, g in globals_.items():
        aws["cdn"] += [{**d, "account": label, "region": "global"} for d in g.get("cdn") or []]
    aws["s3"] = {"count": sum(int((g.get("s3") or {}).get("count") or 0) for g in globals_.values())}
    aws["cost"] = _cost(globals_)
    regions = list(dict.fromkeys(p["region"] for p in parts))
    accounts = list(dict.fromkeys(p["account"] for p in parts))
    aws.update(collectedAt=collected_at, regions=regions, accounts=accounts, region=", ".join(regions) or "none",
               account=accounts[0] if len(accounts) == 1 else f"{len(accounts)} accounts",
               scopes=[{"account": p["account"], "region": p["region"]} for p in parts], skippedScopes=skipped, where=where, counts=counts)
    ts = merge_ts([p["ts"] for p in parts], parts, _daily(globals_)) if parts else {}
    return mask_numbers(aws), mask_numbers(ts)
