# skills/hopscope/scripts/iac_terraform.py
"""Terraform (.tf) → design rows. A small block reader: resource blocks, string/number attributes and
references between resources. Expressions are not evaluated; names built from variables keep their
readable part or fall back to the resource label."""
from __future__ import annotations

import re

import iac_common as c

FILE = re.compile(r"\.tf$")
RESOURCE = re.compile(r'^\s*resource\s+"([\w-]+)"\s+"([\w-]+)"\s*\{', re.M)
NAMED = {"aws_sns_topic": ("sns", "name"), "aws_cognito_user_pool": ("cognito", "name"), "aws_apigatewayv2_api": ("apigw", "name"),
         "aws_api_gateway_rest_api": ("apigw", "name"), "aws_cloudfront_distribution": ("cdn", "comment"), "aws_dynamodb_table": ("dynamodb", "name"),
         "aws_cloudwatch_event_rule": ("eventbridge", "name"), "aws_opensearch_domain": ("search", "domain_name"), "aws_s3_bucket": ("s3", "bucket"),
         "aws_sesv2_email_identity": ("ses", "email_identity")}
Block = tuple[str, str, str]  # type, label, body


HEREDOC = re.compile(r"<<-?([A-Za-z_]\w*)[ \t]*\n")


def _skip_string(text: str, i: int, out: list[str] | None = None) -> int:
    """Copy the string starting at text[i] into out, with each ${...} replaced by ${}; return the index after it."""
    emit = out.append if out is not None else (lambda _x: None)
    emit('"')
    i += 1
    while i < len(text):
        if text.startswith("$${", i):
            emit("$${")
            i += 3
        elif text.startswith("${", i):
            depth, i = 1, i + 2
            while i < len(text) and depth:
                if text[i] == '"':
                    i = _skip_string(text, i)
                    continue
                depth += {"{": 1, "}": -1}.get(text[i], 0)
                i += 1
            emit("${}")
        elif text[i] == "\\":
            emit(text[i:i + 2])
            i += 2
        elif text[i] == '"':
            emit('"')
            return i + 1
        else:
            emit(text[i])
            i += 1
    return i


def clean(text: str) -> str:
    """Drop comments and heredoc bodies (keeping container_definitions ones, which carry the images) and
    reduce interpolations to ${}, so later regexes never meet quotes or braces from them."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            i = _skip_string(text, i, out)
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = len(text) if j < 0 else j + 2
            out.append(" ")
        elif ch == "#" or text.startswith("//", i):
            j = text.find("\n", i)
            i = len(text) if j < 0 else j
        elif text.startswith("<<", i) and (m := HEREDOC.match(text, i)):
            end = re.compile(rf"^[ \t]*{m.group(1)}[ \t]*$", re.M).search(text, m.end())
            line_start = text.rfind("\n", 0, i) + 1
            if re.search(r"container_definitions\s*=\s*$", text[line_start:i]):
                out.append(text[m.end():end.start() if end else len(text)])
            else:
                out.append('""')
            i = len(text) if end is None else end.end()
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def top_level(body: str) -> str:
    """The block body with the contents of nested {...} removed, so attributes read here are the block's own."""
    out: list[str] = []
    depth, i = 0, 0
    while i < len(body):
        ch = body[i]
        if ch == '"':
            tmp: list[str] = []
            j = _skip_string(body, i, tmp)
            if depth == 0:
                out.append("".join(tmp))
            i = j
            continue
        if ch == "{":
            depth += 1
        if depth == 0 or (ch == "}" and depth == 1) or (ch == "{" and depth == 1):
            out.append(ch)
        if ch == "}":
            depth -= 1
        i += 1
    return "".join(out)


def body_at(text: str, open_brace: int) -> str:
    depth, i, in_str = 0, open_brace, False
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "#" or text.startswith("//", i):
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace + 1:i]
        i += 1
    return text[open_brace + 1:]


def blocks(text: str) -> list[Block]:
    """Resource blocks that are switched on (count = 0 means the resource does not exist)."""
    found = [(m.group(1), m.group(2), body_at(text, m.end() - 1)) for m in RESOURCE.finditer(text)]
    return [b for b in found if num(b[2], "count", 1) != 0]


def attr(body: str, key: str) -> str | None:
    m = re.search(rf'^\s*{key}\s*=\s*"((?:[^"\\]|\\.)*)"', top_level(body), re.M)
    return m.group(1) if m else None


def num(body: str, key: str, default: int) -> int:
    m = re.search(rf"^\s*{key}\s*=\s*(\d+)", top_level(body), re.M)
    return int(m.group(1)) if m else default


def ref(body: str, key: str, rtype: str) -> str | None:
    rtype = rtype.replace("aws_lb", "aws_(?:lb|alb)")
    m = re.search(rf"(?<![\w-]){key}\s*=\s*{rtype}\.([\w-]+)", body)
    return m.group(1) if m else None


def target_groups(body: str) -> list[str]:
    """Target groups an action block forwards to, directly or through forward { target_group { arn } }."""
    return re.findall(r"(?<![\w-])(?:target_group_arn|arn)\s*=\s*aws_(?:lb|alb)_target_group\.([\w-]+)", body)


def nested(body: str, name: str) -> list[str]:
    return [body_at(body, m.end() - 1) for m in re.finditer(rf"\b{name}\s*\{{", body)]


def values(body: str) -> list[str]:
    m = re.search(r"values\s*=\s*\[([^\]]*)\]", body)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


def _when(rule_body: str) -> str:
    parts = []
    for cond in nested(rule_body, "condition"):
        for field, label in (("path_pattern", "path-pattern"), ("host_header", "host-header")):
            for inner in nested(cond, field):
                parts += [f"{label}={v}" for v in values(inner)]
    return " & ".join(parts) or "default"


def read(text: str, rel: str) -> dict:
    text = clean(text)
    part: dict = {"ecs": [], "lb": [], "sqs": [], "rds": [], "cache": [], "lambda": [], "sns": [], "cognito": [], "apigw": [], "cdn": [],
                  "dynamodb": [], "eventbridge": [], "search": [], "ses": [], "s3": {"count": 0}}
    bl = blocks(text)
    by = {(t, label): body for t, label, body in bl}
    clusters = {label: c.clean_name(attr(b, "name"), label) for (t, label), b in by.items() if t == "aws_ecs_cluster"}
    tg_names = {label: c.clean_name(attr(b, "name"), label) for (t, label), b in by.items() if t in ("aws_lb_target_group", "aws_alb_target_group")}
    tg_services: dict[str, list[str]] = {}
    for t, label, b in bl:
        if t == "aws_ecs_service":
            name = c.clean_name(attr(b, "name"), label)
            cl = clusters.get(ref(b, "cluster", "aws_ecs_cluster") or "", "terraform")
            td = by.get(("aws_ecs_task_definition", ref(b, "task_definition", "aws_ecs_task_definition") or ""), "")
            images = re.findall(r'"?image"?\s*[=:]\s*"([^"]+)"', td)
            env = dict(re.findall(r'\{\s*"?name"?\s*[=:]\s*"([^"]+)"[\s,]*"?value"?\s*[=:]\s*"([^"]*)"', td))
            tgs = [g for lb_body in nested(b, "load_balancer") if (g := ref(lb_body, "target_group_arn", "aws_lb_target_group"))]
            part["ecs"].append(c.ecs_row(cl, name, desired=num(b, "desired_count", 1), images=images, env_vars=env, build_from=rel,
                                         source="terraform", tgs=[tg_names.get(g, g) for g in tgs]))
            for g in tgs:
                tg_services.setdefault(g, []).append(f"{cl}/{name}")
        elif t == "aws_sqs_queue":
            part["sqs"].append(c.sqs_row(c.clean_name(attr(b, "name"), label), redrive="redrive_policy" in b))
        elif t in ("aws_rds_cluster", "aws_db_instance"):
            name = c.clean_name(attr(b, "cluster_identifier") or attr(b, "identifier"), label)
            part["rds"].append(c.rds_row(name, attr(b, "engine") or "", serverless="serverlessv2_scaling_configuration" in b))
        elif t in ("aws_elasticache_cluster", "aws_elasticache_replication_group"):
            name = c.clean_name(attr(b, "cluster_id") or attr(b, "replication_group_id"), label)
            part["cache"].append(c.cache_row(name, attr(b, "engine") or "redis"))
        elif t == "aws_lambda_function":
            part["lambda"].append(c.lambda_row(c.clean_name(attr(b, "function_name"), label), num(b, "memory_size", 128)))
        elif t in NAMED:
            kind, key = NAMED[t]
            c.add_named(part, kind, c.clean_name(attr(b, key), label))
    listeners = {label: (ref(b, "load_balancer_arn", "aws_lb") or ref(b, "load_balancer_arn", "aws_alb"), num(b, "port", 443), b)
                 for (t, label), b in by.items() if t in ("aws_lb_listener", "aws_alb_listener")}
    lbs = {label: c.lb_row(c.clean_name(attr(b, "name"), label), internal=re.search(r"^\s*internal\s*=\s*true", top_level(b), re.M) is not None)
           for (t, label), b in by.items() if t in ("aws_lb", "aws_alb")}

    tg_types = {label: (attr(b, "target_type") or "ip").lower() for (t, label), b in by.items() if t in ("aws_lb_target_group", "aws_alb_target_group")}
    attach_bodies = [b for t, _l, b in bl if t in ("aws_lb_target_group_attachment", "aws_alb_target_group_attachment", "aws_autoscaling_attachment", "aws_lambda_permission")]

    def add_route(lb_label: str | None, port: int, when: str, body: str) -> None:
        for g in target_groups(body):
            if lb_label in lbs:
                attached = any(re.search(rf"aws_(?:lb|alb)_target_group\.{re.escape(g)}(?![\w-])", ab) for ab in attach_bodies)
                lbs[lb_label]["routes"].append(c.route(when, tg_names.get(g, g), tg_services.get(g, []), port=port,
                                                       tg_type=tg_types.get(g, "ip"), attached=attached))

    for t, _label, b in bl:
        if t in ("aws_lb_listener_rule", "aws_alb_listener_rule"):
            lb_label, port, _ = listeners.get(ref(b, "listener_arn", "aws_lb_listener") or "", (None, 443, ""))
            for action in nested(b, "action"):
                add_route(lb_label, port, _when(b), action)
    for lb_label, port, b in listeners.values():
        for action in nested(b, "default_action"):
            add_route(lb_label, port, "default", action)
    part["lb"] = list(lbs.values())
    return part
