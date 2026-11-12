"""Region and account scopes in a merged inventory. Standard library only.

When several regions or accounts are read in one run (aws_multi.py), a name that two of them share gets a scope prefix
("eu-west-1:orders", "kestrel-prod:orders", "kestrel-prod:eu-west-1:orders"); names nobody else uses stay as they are.
AWS resource names never contain ":", so the plain name is whatever follows the last one."""
from __future__ import annotations


def plain(name: object) -> str:
    """The resource's own name, without a scope prefix: 'eu-west-1:orders' -> 'orders'."""
    return str(name or "").rsplit(":", 1)[-1]


def scoped(row: dict) -> dict:
    """The row's region and account fields, for rows derived from it (empty for a single-scope inventory)."""
    return {k: row[k] for k in ("region", "account") if row.get(k)}


def tag(holders: list[tuple[str, str]], account: str, region: str) -> str:
    """The shortest prefix telling apart the (account, region) scopes that share a name: the region when they are all in
    different regions, else the account when those differ, else both."""
    if len({r for _, r in holders}) == len(holders):
        return region
    if len({a for a, _ in holders}) == len(holders):
        return account
    return f"{account}:{region}"
