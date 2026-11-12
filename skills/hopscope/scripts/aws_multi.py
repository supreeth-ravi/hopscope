"""Read several regions and accounts in one run and merge them into one aws.json and ts.json.

    python aws_multi.py --regions eu-west-1,us-east-1 [--profiles p1,p2] --out data/aws.json --ts-out data/ts.json
    python aws_multi.py --all-regions --org-role HopscopeReadOnly [--accounts id1,id2] --out … --ts-out …

Each (account, region) is collected with the same read-only collectors as aws_inventory.py and aws_timeseries.py, a few at
a time (ThreadPoolExecutor, --workers). A region where nothing is found is left out; a region or account that fails is
skipped with a warning and the run carries on. CloudFront, the S3 bucket count and Cost Explorer are read once per account.
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

import aws_accounts
import aws_inventory
import aws_timeseries
from aws_accounts import Account
from awsutil import session
from iac_common import mask_numbers

MAX_WORKERS = 4
COUNT_KEYS = ("nat", "ecr", "secrets")


def is_empty(inv: dict) -> bool:
    """Nothing deployed: every list empty and every count zero."""
    lists = any(v for v in inv.values() if isinstance(v, list))
    counts = any(int(inv.get(k) or 0) for k in COUNT_KEYS) or int((inv.get("eips") or {}).get("total") or 0)
    return not lists and not counts


def _scope(acct: Account, region: str, staging: str | None) -> dict:
    """One region of one account: its inventory and hourly series, or why it was left out."""
    failures: list[str] = []
    try:
        s = acct.make(region)
        inv = aws_inventory.regional(s, staging, None, include_global=False, failures=failures)
    except Exception as e:  # noqa: BLE001 - one region must not fail the run
        return {"skip": f"{type(e).__name__}: {mask_numbers(str(e))[:120]}"}
    if is_empty(inv):
        failed = [f for f in failures if f]
        if failures and len(failed) == len(failures):  # not an empty region: nothing could be read
            return {"skip": "all collectors failed (" + "; ".join(dict.fromkeys(f.split(": ", 1)[1] for f in failed))[:300] + ")"}
        return {"skip": "nothing found"}
    try:
        ts = aws_timeseries.hourly(s, inv)
    except Exception as e:  # noqa: BLE001 - the inventory is still worth having without charts
        print(f"warning: {acct.label} {region}: time series skipped ({type(e).__name__})")
        ts = {"labels": {}, "series": {}, "byService": []}
    return {"inv": inv, "ts": ts}


def _globals(acct: Account, region: str) -> dict:
    s = acct.make(region)
    g = aws_inventory.safe(f"{acct.label}: CloudFront and S3", aws_inventory.collect_global, s) or {"cdn": [], "s3": {"count": 0}}
    return {**g, "cost": aws_inventory.safe(f"{acct.label}: cost explorer", aws_inventory.collect_cost, s) or [], "dailyCost": aws_timeseries.daily_cost(s)}


def collect_all(accounts: list[Account], regions_of: Callable[[Account], list[str]], staging: str | None,
                workers: int = MAX_WORKERS) -> tuple[list[dict], dict[str, dict], list[dict]]:
    """(parts in account then region order, {label: global services}, skipped scopes with the reason)."""
    jobs: list[tuple[Account, str]] = []
    globals_: dict[str, dict] = {}
    skipped: list[dict] = []
    for acct in accounts:
        try:
            regions = regions_of(acct)
        except Exception as e:  # noqa: BLE001 - ec2:DescribeRegions denied: this account is skipped, not the run
            print(f"warning: {acct.label} skipped: could not list its regions ({type(e).__name__})")
            skipped.append({"account": acct.label, "region": "*", "why": f"could not list regions ({type(e).__name__})"})
            continue
        if regions:
            try:
                globals_[acct.label] = _globals(acct, regions[0])
            except Exception as e:  # noqa: BLE001 - its regions may still be readable
                print(f"warning: {acct.label}: global services skipped ({type(e).__name__})")
                globals_[acct.label] = {"cdn": [], "s3": {"count": 0}, "cost": [], "dailyCost": []}
        jobs += [(acct, r) for r in regions]
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs) or 1))) as pool:
        results = list(pool.map(lambda j: _scope(j[0], j[1], staging), jobs))
    parts = []
    for (acct, region), res in zip(jobs, results, strict=True):
        if "skip" in res:
            if res["skip"] != "nothing found":
                print(f"warning: {acct.label} {region} skipped ({res['skip']})")
            skipped.append({"account": acct.label, "region": region, "why": res["skip"]})
            continue
        inv = res["inv"]
        print(f"  {acct.label} {region}: {len(inv['ecs'])} ECS services, {len(inv['ec2'])} EC2, {len(inv['rds'])} databases, "
              f"{len(inv['lb'])} load balancers, {len(inv['sqs'])} queues, {len(inv['lambda'])} Lambdas")
        parts.append({"account": acct.label, "region": region, **res})
    return parts, globals_, skipped


def accounts_for(a: argparse.Namespace) -> list[Account]:
    profiles: list[str | None] = list(aws_accounts.split_list(a.profiles)) or [None]
    if a.org_role and len(profiles) > 1:
        raise SystemExit("--org-role reads the organization from one signed-in account: pass at most one --profile with it")
    if a.org_role:
        base = session(profiles[0], (aws_accounts.split_list(a.regions) or ["us-east-1"])[0])
        return aws_accounts.from_org(base, a.org_role, aws_accounts.split_list(a.accounts), aws_accounts.boto_from_creds)
    return aws_accounts.from_profiles(profiles, session)


def add_kube(parts: list[dict], kube: list[str], account: str) -> None:
    """kubectl contexts are not tied to an AWS region or profile: each one is its own scope, region "kube:<context>"."""
    import aws_eks

    for ctx in kube:
        rows = aws_inventory.safe(f"kubernetes {ctx}", aws_eks.collect_kube, [ctx]) or {}
        if any(rows.values()):
            parts.append({"account": account, "region": f"kube:{aws_eks.short(ctx)}", "inv": rows, "ts": {"labels": {}, "series": {}, "byService": []}})


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--regions", help="comma-separated regions")
    ap.add_argument("--all-regions", action="store_true", help="every region enabled for each account (ec2:DescribeRegions)")
    ap.add_argument("--profiles", help="comma-separated AWS profiles, one account each")
    ap.add_argument("--org-role", help="opt-in: read every organization account through this read-only role (sts:AssumeRole)")
    ap.add_argument("--accounts", help="with --org-role: only these 12-digit account ids")
    ap.add_argument("--staging-pattern")
    ap.add_argument("--kube-context", action="append", default=[])
    ap.add_argument("--kube-contexts")
    ap.add_argument("--workers", type=int, default=MAX_WORKERS)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ts-out", required=True)
    a = ap.parse_args(argv)
    requested = aws_accounts.regions(None, aws_accounts.split_list(a.regions), False) if a.regions else []
    if not requested and not a.all_regions:
        raise SystemExit("aws_multi needs --regions or --all-regions")
    accounts = accounts_for(a)
    if not accounts:
        raise SystemExit("No account could be read. AWS sign-in needed: run `aws login` (or `aws sso login --profile …`) and try again.")
    first = requested[0] if requested else "us-east-1"
    regions_of: Callable[[Account], list[str]] = ((lambda acct: aws_accounts.regions(acct.make(first), [], True)) if a.all_regions else (lambda acct: requested))
    parts, globals_, skipped = collect_all(accounts, regions_of, a.staging_pattern, max(1, min(a.workers, 16)))
    import aws_eks

    if not parts:
        reasons = "; ".join(f'{x["account"]} {x["region"]}: {x["why"]}' for x in skipped)[:1500]
        raise SystemExit(f"Nothing could be read in any region or account, so no console was built. {reasons or 'No region was listed.'}")
    add_kube(parts, aws_eks.contexts(a.kube_context, a.kube_contexts), accounts[0].label)
    import aws_merge

    aws, ts = aws_merge.merge(parts, globals_, skipped, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    _write(a.out, aws)
    _write(a.ts_out, ts)
    print(f"aws inventory: {len(parts)} region(s) in {len(globals_)} account(s), {len(skipped)} skipped; {len(aws['ecs'])} ECS services, "
          f"{len(aws['lb'])} load balancers, {len(aws['sqs'])} queues, {len(aws['lambda'])} Lambdas -> {a.out}")


def _write(path: str, data: Any) -> None:
    with open(path, "w") as f:
        json.dump(data, f, separators=(",", ":"))


if __name__ == "__main__":
    main()
