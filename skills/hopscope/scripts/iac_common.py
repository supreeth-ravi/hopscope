# skills/hopscope/scripts/iac_common.py
"""Shapes for design mode: infrastructure read from files, laid out exactly like aws.json so the
linker, rules and console work unchanged. Metrics are zero and planned counts stand in for running."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from collections.abc import Collection, Mapping
from typing import Any
from urllib.parse import urlsplit

STAGING = re.compile(r"stag|(^|[-_/])dev([-_/]|$)|(^|[-_/])test([-_/]|$)|(^|[-_/])qa([-_/]|$)|sandbox", re.I)
OWN_REGISTRY = re.compile(r"\.dkr\.ecr\.|\$\{|^\$|<", re.I)  # ECR, or an image name built from a variable
KINDS = (("rds", re.compile(r"(postgres(ql)?|postgis|timescale(db)?|mysql|mariadb|mongo(db)?|aurora)", re.I)),
         ("cache", re.compile(r"(redis|redis-stack|valkey|memcached|keydb)", re.I)),
         ("search", re.compile(r"(opensearch|elasticsearch)", re.I)))


NAME_KEY = re.compile(r"QUEUE|TOPIC|TABLE|BUCKET|STREAM|(^|_)ARN($|_)|CLUSTER|DB_NAME|DATABASE_NAME", re.I)  # values are resource names
HOST_KEY = re.compile(r"HOST|ENDPOINT|ADDR|URL", re.I)  # values are hosts
URL_KEY = re.compile(r"URL", re.I)
SECRET_KEY = re.compile(r"PASS|PW|PWD|SECRET|TOKEN|KEY|PAT|PRIVATE|CREDENTIAL|AUTH|SALT|SIGNATURE|COOKIE|SESSION|DSN|PHRASE|(^|_)(PIN|OTP|CODE|CERT|PEM)($|_)", re.I)
TOKEN_PREFIX = re.compile(r"^(sk|pk|rk)[-_]|^(ghp|gho|ghs|ghu|xox[a-z])|^AKIA|^AIza", re.I)
IPV4 = re.compile(r"\d{1,3}(\.\d{1,3}){3}")
DOTTED_HOST = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,24}")
PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
HOST_PORT = re.compile(r"([A-Za-z0-9.-]+?)(?::(\d{1,5}))?")
SERVICE_NAME = re.compile(r"[a-z][a-z0-9-]{1,18}")
SERVICE_PORTS = {80, 443, 1433, 3000, 3306, 5432, 5672, 6379, 8000, 8080, 8443, 9092, 9200, 9300, 11211, 15672, 27017}
CREDENTIAL_WORDS = {"admin", "root", "user", "username", "guest", "password"}
HOST = re.compile(r"[A-Za-z0-9.-]+")
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _looks_random(value: str) -> bool:
    flat = re.sub(r"[^A-Za-z0-9]", "", value)
    if UUID.fullmatch(value) or (len(flat) >= 16 and re.fullmatch(r"[0-9a-fA-F]+", flat)):
        return True
    return any(re.search(r"[A-Za-z]", r) and re.search(r"\d", r) for r in re.findall(r"[A-Za-z0-9]{20,}", flat))


def _url_host(v: str) -> str | None:
    try:
        host = urlsplit(v).hostname
    except ValueError:
        return None
    return host if host and HOST.fullmatch(host) else None


def _mixed(v: str) -> bool:
    """Letters and digits together in an undotted word: how passwords look, rarely how resources are named."""
    return bool(re.search(r"[A-Za-z]", v) and re.search(r"\d", v)) and not IPV4.fullmatch(v)


def _is_known_host(key: str, v: str, known: Mapping[str, Collection[int]] | Collection[str] | None) -> bool:
    """A bare host that exactly names a service or datastore defined in the same file (db1, web2). A port
    after it must be a well-known one or one that service declares: `admin:1234` could be user:password."""
    m = HOST_PORT.fullmatch(v)
    if not (known and m and m.group(1) in known and not URL_KEY.search(key)) or m.group(1).lower() in CREDENTIAL_WORDS:
        return False
    if m.group(2) is None:
        return True
    declared = known[m.group(1)] if isinstance(known, Mapping) else ()
    return int(m.group(2)) in SERVICE_PORTS or int(m.group(2)) in declared


def _host_ok(key: str, v: str) -> bool:
    """A host or service name, with an optional service port. A bare word with a number after it could be
    user:password, so it only passes with a well-known port."""
    m = HOST_PORT.fullmatch(v)
    if not m:
        return False
    host, port = m.group(1), int(m.group(2)) if m.group(2) else None
    if port is not None and not 1 <= port <= 65535:
        return False
    if "." in host:
        return len(host) <= 60 and len(host.split(".")) <= 6 and bool(IPV4.fullmatch(host) or DOTTED_HOST.fullmatch(host))
    if _mixed(host):
        return False
    if URL_KEY.search(key) or host.lower() in CREDENTIAL_WORDS:
        return False  # a plain word under a *_URL key is not a URL
    if not SERVICE_NAME.fullmatch(host) or len(re.split(r"[-_]", host)) >= 3:
        return False
    return port is None or port in SERVICE_PORTS


def safe_ref(key: str, value: str, known_hosts: Mapping[str, Collection[int]] | Collection[str] | None = None) -> str | None:
    """Allowlist for environment values: queue URLs and AWS hosts (masked), the bare host of a URL, plain
    names under keys that name a resource, and short host names under host keys. Everything else,
    credentials above all, is dropped."""
    import awsutil

    v = str(value or "").strip()
    out = awsutil.safe_value(v)
    if out is None and SCHEME.match(v):
        host = _url_host(v)
        out = awsutil.mask_host(host.lower()) if host else None
    elif out is None and not SECRET_KEY.search(key):
        v = re.sub(r"\$\{[^}]*\}", "", v).strip("-_. ")  # an interpolated prefix leaves only the readable part
        if v and not _looks_random(v) and not TOKEN_PREFIX.match(v):
            if NAME_KEY.search(key):
                out = v if PLAIN.fullmatch(v) and ("." in v or not _mixed(v)) else None
            elif HOST_KEY.search(key) and (_host_ok(key, v) or _is_known_host(key, v, known_hosts)):
                out = v
    return re.sub(r"\b\d{12}\b", "************", out) if out else None


def env_of(name: str) -> str:
    return "staging" if STAGING.search(name) else "prod"


def clean_name(raw: Any, fallback: str) -> str:
    """A readable resource name: interpolations removed, leftover separators trimmed."""
    text = re.sub(r"\$\{[^}]*\}", "", str(raw or "")).strip("-_. ")
    return text or fallback


def image_engine(image: str) -> str:
    """The repository name of an image: bitnami/postgresql:16 -> postgresql."""
    return str(image or "").split("@")[0].rsplit("/", 1)[-1].split(":")[0]


def infra_kind(image: str) -> str | None:
    name = image_engine(image)
    for kind, rx in KINDS:
        if rx.fullmatch(name):
            return kind
    return None


def as_int(v: Any, default: int) -> int:
    """An integer from a plain number or numeric string; the default for anything else (variables, refs, bools)."""
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        return default
    try:
        return int(v)
    except ValueError:
        return default


def empty_inventory(region: str) -> dict:
    return {"mode": "design", "collectedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "region": region, "account": "",
            "ecs": [], "ec2": [], "rds": [], "lb": [], "sqs": [], "lambda": [], "cost": [], "sns": [], "cache": [], "search": [],
            "apigw": [], "cdn": [], "cognito": [], "s3": {"count": 0}, "nat": 0, "eips": {"total": 0, "unattached": 0}, "ecr": 0,
            "secrets": 0, "dynamodb": [], "ses": [], "eventbridge": [], "sources": [], "skipped": []}


def ecs_row(cluster: str, name: str, desired: int = 1, images: list[str] | None = None, env_vars: dict[str, Any] | None = None,
            build: str | None = None, build_from: str = "", source: str = "", size: str = "", tgs: list[str] | None = None,
            known_hosts: Mapping[str, Collection[int]] | Collection[str] | None = None) -> dict:
    imgs = [i for i in (images or []) if i]
    own = build is not None or not imgs or any(OWN_REGISTRY.search(i) for i in imgs)
    return {"cluster": cluster, "n": name, "running": desired, "desired": desired, "size": size, "images": imgs, "ownImage": own,
            "containers": [name], "refs": [{"k": k, "v": sv} for k, v in sorted((env_vars or {}).items()) if (sv := safe_ref(k, str(v), known_hosts)) is not None], "tgs": list(tgs or []),
            "env": env_of(f"{cluster}/{name}"), "cpu": 0, "mem": 0, "memMax": 0, "cpuMax": 0,
            "build": build, "buildFrom": build_from, "source": source}


def lb_row(name: str, internal: bool = False, source: str = "") -> dict:
    return {"source": source, "n": name, "type": "application", "scheme": "internal" if internal else "internet-facing", "req": 0, "err": 0, "e503": 0, "t5xx": 0, "routes": []}


def route(when: str, tg: str, services: list[str], port: int = 443, tg_type: str = "ip", attached: bool = False) -> dict:
    """tgType and attached are design-only extras: they tell a route served by something outside the files (a
    Lambda, instances, another load balancer) from one that leads nowhere."""
    return {"port": port, "when": when, "tg": tg, "services": services, "targets": [], "instances": [], "tgType": tg_type, "attached": attached}


def sqs_row(name: str, redrive: bool = False) -> dict:
    dlq = bool(re.search(r"dlq|dead[-_]?letter", name, re.I))
    return {"n": name, "sent": 0, "backlog": 0.0, "age": 0.0, "dlq": dlq, "fifo": name.endswith(".fifo"), "fam": name, "redrive": redrive}


LABEL = re.compile(r"[A-Za-z0-9._ -]{1,40}")


def label(v: Any) -> str:
    """A short descriptive word (engine, class) read from a file: letters, digits and . _ - only, else empty."""
    t = str(v or "").strip()
    return t if LABEL.fullmatch(t) else ""


def rds_row(name: str, engine: str, endpoint: str = "", serverless: bool = False) -> dict:
    return {"n": name, "instance": name, "engine": label(engine), "class": "db.serverless" if serverless else "", "serverless": serverless,
            "minAcu": None, "maxAcu": None, "acu": 0, "acuPeak": 0, "cpu": 0, "conns": 0, "endpoint": endpoint or name,
            "clusterEndpoint": "", "env": env_of(name)}


def cache_row(name: str, engine: str = "redis") -> dict:
    return {"n": name, "type": "", "engine": label(engine), "cpu": 0, "mem": 0, "conns": 0}


def lambda_row(name: str, mem: int = 128) -> dict:
    return {"n": name, "mem": mem, "inv": 0, "err": 0.0, "dur": 0.0, "fam": name}


def add_named(inv: dict, kind: str, name: str) -> None:
    """Resources the console only lists by name."""
    if kind == "sns":
        inv["sns"].append({"n": name, "published": 0})
    elif kind == "cognito":
        inv["cognito"].append({"n": name})
    elif kind == "apigw":
        inv["apigw"].append({"n": name, "proto": "HTTP", "req": 0, "fam": name})
    elif kind == "cdn":
        inv["cdn"].append({"n": name, "origin": "", "aliases": []})
    elif kind == "search":
        inv["search"].append({"n": name})
    elif kind == "s3":
        inv["s3"]["count"] += 1
    elif kind in ("dynamodb", "eventbridge", "ses"):
        inv[kind].append(name)
    else:
        raise ValueError(f"unknown kind {kind}")


def merge(into: dict, part: dict) -> None:
    """Add one reader's rows to the inventory, skipping exact duplicates by name."""
    for k, v in part.items():
        if k == "s3":
            into["s3"]["count"] += v["count"]
        elif isinstance(v, list):
            seen = {json_key(x) for x in into[k]}
            into[k].extend(x for x in v if json_key(x) not in seen)


def json_key(x: Any) -> str:
    if isinstance(x, dict):
        return f'{x.get("cluster", "")}/{x.get("n", "")}@{x.get("src") or x.get("buildFrom", "")}'
    return str(x)
