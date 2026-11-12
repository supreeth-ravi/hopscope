"""Drift helpers: name matching, grouping resources into families, and the shape of a finding. Standard library only."""
from __future__ import annotations

import re

from scopes import plain

ENV_WORDS = {"prod", "production", "prd", "live", "staging", "stage", "stg", "dev", "develop", "development", "test", "testing", "qa", "uat",
             "sandbox", "preview", "default"}
KINDS = {"ecs": "ECS service", "sqs": "queue", "rds": "database", "lambda": "Lambda", "cache": "cache", "lb": "load balancer"}
LEVELS = {"exact": 4, "env": 3, "prefix": 2, "low": 1}
MAX_NAMES = 5


def words(name: str) -> list[str]:
    """Name words, without a merged inventory's region or account prefix ("eu-west-1:orders" is the orders queue)."""
    return [w for w in re.split(r"[^a-z0-9]+", re.sub(r"\.fifo$", "", plain(name).lower())) if w]


def canon(name: str) -> list[str]:
    """Name words without environment words and plain numbers: prod-shop-api-01 -> [shop, api]."""
    return [w for w in words(name) if w not in ENV_WORDS and not w.isdigit()] or words(name)


def level(a: str, b: str) -> str | None:
    """How alike two names are: exact, env (equal once environment words and numbers go), prefix (one is the other's
    tail), low (words mostly shared), or None."""
    if a == b or plain(a) == plain(b):
        return "exact"
    ca, cb = canon(a), canon(b)
    if ca == cb:
        return "env"
    short, long_ = (ca, cb) if len(ca) < len(cb) else (cb, ca)
    if short and long_[-len(short):] == short:
        return "prefix"
    sa, sb = set(ca), set(cb)
    if {w for w in sa & sb if len(w) >= 3} and len(sa & sb) / len(sa | sb) >= 0.5:
        return "low"
    return None


def match(name: str, candidates: list[str]) -> tuple[str | None, str | None]:
    """The best candidate and how sure the match is. Several equally good candidates that are different resources (not
    copies of one name in several environments) make the match "low"."""
    scored = [(LEVELS[lv], c) for c in candidates if (lv := level(name, c))]
    if not scored:
        return None, None
    top = max(s for s, _ in scored)
    best = [c for s, c in scored if s == top]
    lv = next(k for k, v in LEVELS.items() if v == top)
    if len({"-".join(canon(c)) for c in best}) > 1:
        lv = "low"
    return best[0], lv


def key(kind: str, row: dict) -> str:
    return f'{row.get("cluster")}/{row.get("n")}' if kind == "ecs" else str(row.get("n") or "")


def group(kind: str, rows: list[dict]) -> list[dict]:
    """Rows as report groups: one per per-tenant family (a `fam` with *), and one for all the other rows of the kind."""
    fams: dict[str, list[str]] = {}
    singles: list[str] = []
    for r in rows:
        fam = str(r.get("fam") or "")
        (fams.setdefault(fam, []) if "*" in fam else singles).append(key(kind, r))
    out: list[dict] = [{"kind": kind, "family": f, "names": sorted(ns)} for f, ns in sorted(fams.items())]
    if singles:
        out.append({"kind": kind, "family": None, "names": sorted(singles)})
    return out


def plural(kind: str, count: int) -> str:
    word = KINDS.get(kind, kind)
    return word if count == 1 else word + "s"


def finding(kind: str, title: str, detail: str, evidence: list[str], resource: str, rkind: str, confidence: str = "high",
            severity: str = "warn", names: list[str] | None = None, family: bool = False) -> dict:
    names = names if names is not None else [resource]
    return {"kind": kind, "severity": severity, "title": title, "detail": detail, "evidence": evidence[:8], "resource": resource,
            "resourceKind": rkind, "confidence": confidence, "count": len(names), "names": names[:MAX_NAMES], "family": family}


def grouped_finding(kind: str, g: dict, where: str, rkind: str, detail: str, other: str) -> dict:
    """One finding for a family or for all the single resources of a kind, naming up to five."""
    names, count = g["names"], len(g["names"])
    shown = ", ".join(names[:MAX_NAMES]) + (f" and {count - MAX_NAMES} more" if count > MAX_NAMES else "")
    if g["family"]:
        resource, label = g["family"], f'{KINDS[rkind]} family {g["family"]} ({count} {plural(rkind, count)})'
    elif count == 1:
        resource, label = names[0], f"{KINDS[rkind]} {names[0]}"
    else:
        resource, label = f"{count} {plural(rkind, count)}", f"{count} {plural(rkind, count)}"
    title = f"{label[0].upper()}{label[1:]}: {where}"
    return finding(kind, title, detail, [f"{plural(rkind, count)}: {shown}", other], resource, rkind, names=names, family=bool(g["family"]))


def describe(findings: list[dict]) -> str:
    """Grouped findings as counts: '1 queue family (14 queues), 2 ECS services, 1 database'."""
    parts: list[str] = []
    fams: dict[str, list[int]] = {}
    singles: dict[str, int] = {}
    for f in findings:
        k = f.get("resourceKind", "")
        if f.get("family"):
            fams.setdefault(k, []).append(int(f.get("count") or 1))
        else:
            singles[k] = singles.get(k, 0) + int(f.get("count") or 1)
    for k, sizes in fams.items():
        parts.append(f'{len(sizes)} {KINDS.get(k, k)} famil{"y" if len(sizes) == 1 else "ies"} ({sum(sizes)} {plural(k, sum(sizes))})')
    for k in [*KINDS, *[x for x in singles if x not in KINDS]]:
        if singles.get(k):
            parts.append(f"{singles[k]} {plural(k, singles[k])}")
    return ", ".join(parts)
