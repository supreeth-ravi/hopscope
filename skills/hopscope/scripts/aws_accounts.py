"""Which accounts and regions one run reads: profiles (one account each), an opt-in Organizations role, enabled regions.

Read-only throughout. With --org-role, the signed-in (management or delegated administrator) account lists the organization's
accounts (organizations:ListAccounts) and assumes `arn:<partition>:iam::<id>:role/<RoleName>` in each one with
sts:AssumeRole (session name "hopscope", one hour). Account numbers are only held in memory: every account is shown by a
label, its alias (iam:ListAccountAliases) when allowed, else `acct-••••1234`. An account that can't be read is skipped
with a warning that names only its label.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable

from awsutil import paginate
from iac_common import mask_numbers

REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d{1,2}", re.ASCII)  # always fullmatch: a trailing newline is not a region
ROLE_NAME = re.compile(r"[\w+=,.@-]{1,64}", re.ASCII)
ACCOUNT_ID = re.compile(r"\d{12}", re.ASCII)
ALIAS = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?", re.ASCII)
POLICY_FILE = Path(__file__).resolve().parent.parent / "references" / "iam-policy.json"
SESSION_NAME, ROLE_SECONDS = "hopscope", 3600


@dataclass
class Account:
    """One account to read: its label for output, and a factory for a boto3 session in a region (thread-safe: a new
    session per call)."""

    label: str
    make: Callable[[str | None], Any]


def split_list(value: str | None) -> list[str]:
    """'a, b,,a' -> ['a', 'b']: comma-separated flags, blanks and repeats dropped, order kept."""
    out: list[str] = []
    for x in str(value or "").split(","):
        if x.strip() and x.strip() not in out:
            out.append(x.strip())
    return out


def warn(msg: str) -> None:
    print(f"warning: {mask_numbers(msg)}")


def regions(s: Any, requested: list[str], all_regions: bool) -> list[str]:
    """The named regions, or every region enabled for the account (ec2:DescribeRegions without opt-in regions left off)."""
    if all_regions:
        found = s.client("ec2").describe_regions(AllRegions=False).get("Regions") or []
        return sorted({str(r.get("RegionName")) for r in found if REGION.fullmatch(str(r.get("RegionName") or ""))})
    bad = [r for r in requested if not REGION.fullmatch(r)]
    if bad:
        raise SystemExit(f"not an AWS region: {', '.join(repr(b[:40]) for b in bad)} (expected names like eu-west-1)")
    return list(dict.fromkeys(requested))


def mask_account(account_id: str) -> str:
    return f"acct-••••{str(account_id)[-4:]}"


def label_for(s: Any, account_id: str) -> str:
    """The account's alias when the role may read it and it is a plain alias, else the masked number."""
    try:
        aliases = s.client("iam").list_account_aliases().get("AccountAliases") or []
    except Exception:  # noqa: BLE001 - iam:ListAccountAliases is optional
        aliases = []
    alias = str(aliases[0]) if aliases else ""
    return alias if ALIAS.fullmatch(alias) else mask_account(account_id)


def _unique(accounts: list[Account]) -> list[Account]:
    seen: dict[str, int] = {}
    for a in accounts:
        n = seen[a.label] = seen.get(a.label, 0) + 1
        if n > 1:
            a.label = f"{a.label}-{n}"
    return accounts


def from_profiles(profiles: list[str | None], make_session: Callable[[str | None, str | None], Any]) -> list[Account]:
    """One account per profile (None = the default credentials). A profile that fails, or names an account already read
    through an earlier profile, is skipped with a warning."""
    out: list[Account] = []
    ids: set[str] = set()
    for p in profiles:
        try:
            s = make_session(p, None)
            account_id = str(s.client("sts").get_caller_identity()["Account"])
        except Exception as e:  # noqa: BLE001 - one profile that can't sign in must not stop the others
            warn(f"profile {p or 'default'} skipped: AWS sign-in failed ({type(e).__name__}). Run `aws sso login --profile {p or 'default'}`.")
            continue
        if account_id in ids:
            warn(f"profile {p or 'default'} skipped: same account ({mask_account(account_id)}) as an earlier profile")
            continue
        ids.add(account_id)
        out.append(Account(label_for(s, account_id), partial(make_session, p)))
    return _unique(out)


def session_policy() -> str:
    """An inline session policy of the read-only actions in references/iam-policy.json: the assumed session can do no more
    than that, even when the role itself allows more."""
    doc = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    actions = sorted({a for s in doc.get("Statement") or [] if s.get("Effect") == "Allow" for a in s.get("Action") or []
                      if a.split(":")[-1].startswith(("Describe", "List", "Get"))} | {"apigateway:GET"})
    return json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": actions, "Resource": "*"}]}, separators=(",", ":"))


def from_org(base: Any, role: str, only: list[str], from_creds: Callable[[dict, str | None], Any]) -> list[Account]:
    """Every active account of the organization (or the ones in `only`), each through the read-only role `role`.

    from_creds(credentials, region) makes a session from assumed-role credentials (a new one per call)."""
    if not ROLE_NAME.fullmatch(role):
        raise SystemExit(f"--org-role must be an IAM role name, not {role[:64]!r}")
    bad = [a for a in only if not ACCOUNT_ID.fullmatch(a)]
    if bad:
        raise SystemExit(f"--accounts takes 12-digit account ids; {len(bad)} value(s) are not")
    sts = base.client("sts")
    try:
        partition = str(sts.get_caller_identity()["Arn"]).split(":")[1] or "aws"
        listed = paginate(base.client("organizations"), "list_accounts", "Accounts")
    except Exception as e:  # noqa: BLE001 - the signed-in account must be the management or a delegated administrator
        raise SystemExit(f"--org-role needs organizations:ListAccounts from the management or a delegated administrator account ({type(e).__name__}).") from e
    active = [str(a.get("Id")) for a in listed if a.get("Status", "ACTIVE") == "ACTIVE" and ACCOUNT_ID.fullmatch(str(a.get("Id") or ""))]
    for missing in (a for a in only if a not in active):
        warn(f"account {mask_account(missing)} skipped: not an active account of this organization")
    out: list[Account] = []
    for account_id in active:
        if only and account_id not in only:
            continue
        try:
            creds = sts.assume_role(RoleArn=f"arn:{partition}:iam::{account_id}:role/{role}", RoleSessionName=SESSION_NAME,
                                    DurationSeconds=ROLE_SECONDS, Policy=session_policy())["Credentials"]
            label = label_for(from_creds(creds, None), account_id)
        except Exception as e:  # noqa: BLE001 - an account without the role is skipped, the rest still run
            warn(f"account {mask_account(account_id)} skipped: could not assume {role} ({type(e).__name__})")
            continue
        out.append(Account(label, partial(from_creds, creds)))
    return _unique(out)


def boto_from_creds(creds: dict, region: str | None) -> Any:
    import boto3  # imported here so design mode and tests never need boto3

    return boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                         aws_session_token=creds["SessionToken"], region_name=region)
