"""Health rules for workflows and streams (live mode): Step Functions executions that failed, Kinesis readers falling
behind, and Lambda event source mappings that are off or failing. Thresholds can be overridden in hopscope.json."""
from __future__ import annotations

from typing import Any

THRESHOLDS = {"sfn_failed_bad": 5, "kinesis_age_warn_ms": 60_000, "kinesis_age_bad_ms": 3_600_000}
MAX_EACH = 3


def _age(ms: float) -> str:
    return f"{ms / 3_600_000:.1f} hours" if ms >= 3_600_000 else f"{ms / 60_000:.0f} minutes"


def _sfn(inv: dict, th: dict) -> list[list]:
    out = []
    for m in sorted((m for m in inv.get("sfn") or [] if isinstance(m, dict) and (m.get("failed") or 0) > 0), key=lambda m: -m["failed"])[:MAX_EACH]:
        level = "bad" if m["failed"] >= th["sfn_failed_bad"] else "warn"
        started = f' of {m["started"]:.0f} started' if m.get("started") else ""
        out.append([level, f'{m["n"]}: {m["failed"]:.0f} Step Functions executions failed in 3 days', f"{m['failed']:.0f}{started}; each one stopped part-way through its workflow.",
                    "Open a failed execution in the Step Functions console: the failing state and its error say what to fix, then redrive it."])
    return out


def _kinesis(inv: dict, th: dict) -> list[list]:
    out = []
    late = [k for k in inv.get("kinesis") or [] if isinstance(k, dict) and (k.get("iteratorAgeMs") or 0) >= th["kinesis_age_warn_ms"]]
    for k in sorted(late, key=lambda k: -k["iteratorAgeMs"])[:MAX_EACH]:
        level = "bad" if k["iteratorAgeMs"] >= th["kinesis_age_bad_ms"] else "warn"
        out.append([level, f'Readers of Kinesis stream {k["n"]} are {_age(k["iteratorAgeMs"])} behind', "Records wait that long before a consumer reads them; past the retention period they are lost.",
                    "Check the consumer's errors and throttling, then add shards or raise its batch size or parallelism."])
    return out


def _waiting(inv: dict, kind: str, source: str) -> bool:
    """Messages still arrive at (or wait in) the source a disabled mapping would read, as far as the inventory says."""
    if kind == "sqs":
        q = next((q for q in inv.get("sqs") or [] if isinstance(q, dict) and q.get("n") == source), None)
        return bool(q and ((q.get("sent") or 0) > 0 or (q.get("backlog") or 0) > 0))
    if kind == "kinesis":
        k = next((k for k in inv.get("kinesis") or [] if isinstance(k, dict) and k.get("n") == source), None)
        return bool(k and (k.get("incoming") or 0) > 0)
    return False


def _esm(inv: dict) -> list[list]:
    """Failing mappings one by one; disabled ones as a single item, and only when messages still arrive for one of them
    (a mapping paused on a quiet source is usually deliberate; Components lists them all)."""
    out = []
    maps = [m for m in inv.get("esm") or [] if isinstance(m, dict)]
    for m in maps:
        if str(m.get("last") or "").startswith("PROBLEM"):
            out.append(["bad", f'{m.get("fn")} can\'t process {m.get("source")}', str(m.get("last"))[:200],
                        "Fix the function's error or its permissions on the source; records keep retrying until they expire."])
    out = out[:MAX_EACH * 2]
    off = [m for m in maps if str(m.get("state") or "") in ("Disabled", "Disabling")]
    busy = [m for m in off if _waiting(inv, str(m.get("kind") or ""), str(m.get("source") or ""))]
    if busy:
        names = ", ".join(f'{m.get("fn")} ({m.get("source")})' for m in busy[:8]) + (f" and {len(busy) - 8} more" if len(busy) > 8 else "")
        quiet = len(off) - len(busy)
        out.append(["warn", f"{len(busy)} event source{'s are' if len(busy) != 1 else ' is'} disabled while messages still arrive",
                    f"{names}: their messages pile up unread." + (f" {quiet} more are disabled with nothing waiting." if quiet else ""),
                    "Enable each mapping again if its function should still consume the source, or delete it."])
    return out


def actions(inv: dict, th: dict[str, Any]) -> list[list]:
    th = {**THRESHOLDS, **th}
    out = _sfn(inv, th) + _kinesis(inv, th) + _esm(inv)
    return sorted(out, key=lambda a: {"bad": 0, "warn": 1}.get(a[0], 2))
