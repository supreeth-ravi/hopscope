"""Shared AWS helpers: session, paginated listing, batched CloudWatch queries, environment naming."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

STAGING = re.compile(r"(^|[-_./])(stag|staging|stg|dev|development|test|qa|sandbox|preview)([-_./]|$)", re.I)
# Environment values worth keeping: queue URLs and AWS endpoint hostnames only. Everything else may be a secret.
QUEUE_URL = re.compile(r"^https://sqs\.[a-z0-9-]+\.amazonaws\.com/\d{12}/[\w.-]+$")
# The endpoint must be the whole host: optional port and a plain path after it, nothing else (no query strings).
AWS_HOST = re.compile(r"^([a-z0-9][\w.-]*\.(?:rds|cache|es|aoss)\.amazonaws\.com)(?::\d{1,5})?(?:/[\w./-]*)?$", re.I)


def safe_value(value: str) -> str | None:
    """A queue URL as-is, or just the hostname of a database/cache/search endpoint; None for anything else.

    Connection strings such as postgres://user:password@host/db are reduced to the bare host, so
    credentials, paths and query strings are never stored.
    """
    v = str(value or "").strip()
    if QUEUE_URL.match(v):
        return v
    host_part = re.sub(r"^[a-z][a-z0-9+.-]*://", "", v, flags=re.I)  # drop scheme://
    host_part = re.sub(r"^[^@/]*@", "", host_part)  # drop user:password@
    m = AWS_HOST.match(host_part)
    return m.group(1).lower() if m else None


def session(profile: str | None = None, region: str | None = None) -> boto3.Session:
    return boto3.Session(profile_name=profile, region_name=region)


def env_of(name: str, pattern: str | None = None) -> str:
    rx = re.compile(pattern, re.I) if pattern else STAGING
    return "staging" if rx.search(name or "") else "prod"


def paginate(client: Any, op: str, key: str, **kw: Any) -> list[Any]:
    out = []
    for page in client.get_paginator(op).paginate(**kw):
        out.extend(page.get(key, []))
    return out


def window(days: int = 3) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return end - timedelta(days=days), end


Query = tuple[str, str, str, dict[str, str], str]


def metric_totals(cw: Any, queries: list[Query], days: int = 3) -> dict[str, float]:
    """queries: [(id, namespace, metric, {dim: value}, stat)] -> {id: value over the whole window}.

    Sum stats are summed, Maximum is the max, everything else (Average, pXX) is the max of hourly values.
    """
    start, end = window(days)
    period = days * 86400
    out = {}
    for i in range(0, len(queries), 500):
        batch = queries[i:i + 500]
        req = [{"Id": q[0], "MetricStat": {"Metric": {"Namespace": q[1], "MetricName": q[2],
                "Dimensions": [{"Name": k, "Value": v} for k, v in q[3].items()]}, "Period": period, "Stat": q[4]}} for q in batch]
        token = None
        while True:
            kw = {"MetricDataQueries": req, "StartTime": start, "EndTime": end}
            if token:
                kw["NextToken"] = token
            resp = cw.get_metric_data(**kw)
            for r in resp["MetricDataResults"]:
                vals = r.get("Values", [])
                out[r["Id"]] = round(max(vals), 3) if vals else 0
            token = resp.get("NextToken")
            if not token:
                break
    return out


def metric_hourly(cw: Any, queries: list[Query], days: int = 3) -> tuple[dict[str, list[float]], str]:
    """queries as above -> ({id: [72 hourly values]}, start_iso)."""
    start, end = window(days)
    hours = [(start + timedelta(hours=h)).isoformat() for h in range(days * 24)]
    out: dict[str, dict[str, float]] = {}
    for i in range(0, len(queries), 400):
        req = [{"Id": q[0], "MetricStat": {"Metric": {"Namespace": q[1], "MetricName": q[2],
                "Dimensions": [{"Name": k, "Value": v} for k, v in q[3].items()]}, "Period": 3600, "Stat": q[4]}} for q in queries[i:i + 400]]
        token = None
        while True:
            kw = {"MetricDataQueries": req, "StartTime": start, "EndTime": end, "ScanBy": "TimestampAscending"}
            if token:
                kw["NextToken"] = token
            resp = cw.get_metric_data(**kw)
            for r in resp["MetricDataResults"]:
                # CloudWatch returns local-time timestamps; normalise to UTC hours before matching.
                by = {t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat(): v for t, v in zip(r["Timestamps"], r["Values"], strict=False)}
                prev = out.setdefault(r["Id"], {})
                prev.update(by)
            token = resp.get("NextToken")
            if not token:
                break
    return {k: [round(v.get(h, 0), 4) for h in hours] for k, v in out.items()}, start.isoformat()


def family(name: str) -> str:
    """Collapse per-tenant resources (queue_<hex>.fifo, tool_<hex>) into one family name."""
    return re.sub(r"[_-]?[0-9a-f]{8,}", "_*", name)
