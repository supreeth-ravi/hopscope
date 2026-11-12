"""Several accounts and regions: profiles, opt-in Organizations roles, enabled regions and account labels (aws_accounts.py).
Stubbed boto3 only: no AWS account, Organizations or STS call is ever made."""
from __future__ import annotations

import json

import pytest

import aws_accounts

ACCT_A = "1" * 8 + "4321"  # account numbers built at runtime: no account-shaped literal in the source
ACCT_B = "2" * 8 + "8765"
ACCT_C = "3" * 8 + "1111"
MGMT = "9" * 12


class Pager:
    def __init__(self, pages: list[dict]):
        self.pages = pages

    def paginate(self, **kw):
        yield from self.pages


class STS:
    def __init__(self, account: str, fail_for: set[str] | None = None, partition: str = "aws"):
        self.account, self.fail_for, self.partition = account, fail_for or set(), partition
        self.assumed: list[dict] = []

    def get_caller_identity(self):
        return {"Account": self.account, "Arn": f"arn:{self.partition}:sts::{self.account}:assumed-role/ops/kestrel"}

    def assume_role(self, **kw):
        self.assumed.append(kw)
        acct = kw["RoleArn"].split(":")[4]
        if acct in self.fail_for:
            raise PermissionError(f"AccessDenied: not authorized to perform sts:AssumeRole on {kw['RoleArn']}")
        return {"Credentials": {"AccessKeyId": "AK" + "TESTONLY" + acct[-4:], "SecretAccessKey": "fake-" + "secret", "SessionToken": "fake-" + "token"}}


class IAM:
    def __init__(self, aliases: list[str] | Exception):
        self.aliases = aliases

    def list_account_aliases(self):
        if isinstance(self.aliases, Exception):
            raise self.aliases
        return {"AccountAliases": self.aliases}


class Orgs:
    def __init__(self, accounts: list[tuple[str, str]]):
        self.accounts = accounts

    def get_paginator(self, op):
        assert op == "list_accounts"
        return Pager([{"Accounts": [{"Id": i, "Status": st, "Name": "invented"} for i, st in self.accounts]}])


class EC2:
    def describe_regions(self, **kw):
        assert kw.get("AllRegions") is False  # only the regions enabled for the account
        return {"Regions": [{"RegionName": "us-east-1"}, {"RegionName": "eu-west-1"}, {"RegionName": "ap-south-1"}]}


class Session:
    def __init__(self, **clients):
        self.clients = clients

    def client(self, name, **kw):
        return self.clients[name]


def test_split_list_and_region_validation() -> None:
    assert aws_accounts.split_list(" eu-west-1, us-east-1 ,,eu-west-1") == ["eu-west-1", "us-east-1"]
    assert aws_accounts.split_list(None) == []
    assert aws_accounts.regions(None, ["eu-west-1", "us-gov-west-1"], False) == ["eu-west-1", "us-gov-west-1"]
    with pytest.raises(SystemExit):
        aws_accounts.regions(None, ["eu-west-1; rm -rf"], False)


def test_all_regions_are_the_enabled_ones_sorted() -> None:
    assert aws_accounts.regions(Session(ec2=EC2()), [], True) == ["ap-south-1", "eu-west-1", "us-east-1"]


def test_labels_alias_or_masked_never_the_full_number() -> None:
    assert aws_accounts.mask_account(ACCT_A) == "acct-••••4321"
    assert aws_accounts.label_for(Session(iam=IAM(["kestrel-prod"])), ACCT_A) == "kestrel-prod"
    assert aws_accounts.label_for(Session(iam=IAM(PermissionError("AccessDenied"))), ACCT_A) == "acct-••••4321"
    assert aws_accounts.label_for(Session(iam=IAM(["<script>"])), ACCT_A) == "acct-••••4321"  # an alias is a plain name or nothing


def test_profiles_one_account_each_and_failures_skipped(capsys) -> None:
    sessions = {"prod": Session(sts=STS(ACCT_A), iam=IAM(["kestrel-prod"])), "stage": Session(sts=STS(ACCT_B), iam=IAM([])),
                "same": Session(sts=STS(ACCT_A), iam=IAM(["kestrel-prod"]))}

    def make(profile, region):
        if profile == "broken":
            raise RuntimeError(f"The config profile could not be found for {ACCT_C}")
        return sessions[profile]

    accts = aws_accounts.from_profiles(["prod", "stage", "same", "broken"], make)
    assert [a.label for a in accts] == ["kestrel-prod", "acct-••••8765"]
    out = capsys.readouterr().out
    assert "broken" in out and "same" in out and ACCT_C not in out and ACCT_A not in out


def test_org_role_assumes_read_only_role_in_each_member(capsys) -> None:
    sts = STS(MGMT, fail_for={ACCT_B})
    base = Session(sts=sts, organizations=Orgs([(ACCT_A, "ACTIVE"), (ACCT_B, "ACTIVE"), (ACCT_C, "SUSPENDED")]))
    made: list[dict] = []

    def from_creds(creds, region):
        made.append({"creds": creds, "region": region})
        return Session(iam=IAM(["kestrel-shop"] if creds["AccessKeyId"].endswith("4321") else []))

    accts = aws_accounts.from_org(base, "HopscopeReadOnly", [], from_creds)
    assert [a.label for a in accts] == ["kestrel-shop"]
    assert {k: v for k, v in sts.assumed[0].items() if k != "Policy"} == {"RoleArn": f"arn:aws:iam::{ACCT_A}:role/HopscopeReadOnly", "RoleSessionName": "hopscope",
                                                                       "DurationSeconds": 3600}
    assert json.loads(sts.assumed[0]["Policy"])["Statement"][0]["Effect"] == "Allow"  # a read-only session policy (test_review_080)
    assert len(sts.assumed) == 2  # the suspended account is not tried
    out = capsys.readouterr().out
    assert "acct-••••8765 skipped" in out and ACCT_B not in out
    accts[0].make("eu-west-1")
    assert made[-1]["region"] == "eu-west-1"


def test_org_role_limited_to_named_accounts_and_partition_kept(capsys) -> None:
    sts = STS(MGMT, partition="aws-us-gov")
    base = Session(sts=sts, organizations=Orgs([(ACCT_A, "ACTIVE"), (ACCT_B, "ACTIVE")]))
    accts = aws_accounts.from_org(base, "HopscopeReadOnly", [ACCT_B, ACCT_C], lambda c, r: Session(iam=IAM([])))
    assert [a.label for a in accts] == ["acct-••••8765"]
    assert sts.assumed[0]["RoleArn"].startswith("arn:aws-us-gov:iam::")
    out = capsys.readouterr().out
    assert "acct-••••1111" in out and ACCT_C not in out  # named but not in the organization


def test_org_role_name_and_account_ids_are_validated() -> None:
    base = Session(sts=STS(MGMT), organizations=Orgs([]))
    with pytest.raises(SystemExit):
        aws_accounts.from_org(base, "Bad Role/../x", [], lambda c, r: None)
    with pytest.raises(SystemExit):
        aws_accounts.from_org(base, "HopscopeReadOnly", ["12345"], lambda c, r: None)


def test_duplicate_masked_labels_stay_distinct() -> None:
    a = "4" * 8 + "4321"
    sessions = {"x": Session(sts=STS(ACCT_A), iam=IAM([])), "y": Session(sts=STS(a), iam=IAM([]))}
    accts = aws_accounts.from_profiles(["x", "y"], lambda p, r: sessions[p])
    assert [x.label for x in accts] == ["acct-••••4321", "acct-••••4321-2"]
    assert ACCT_A not in json.dumps([x.label for x in accts])
