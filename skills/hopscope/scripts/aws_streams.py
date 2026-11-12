"""Workflows and streams, read-only: Step Functions (states:ListStateMachines/DescribeStateMachine, with the services its
definition calls and failed executions from CloudWatch), Kinesis Data Streams (ListStreams/DescribeStreamSummary,
IncomingRecords and GetRecords.IteratorAgeMilliseconds), MSK clusters (kafka:ListClustersV2; topics are not read),
EventBridge rule targets (events:ListTargetsByRule) and Lambda event source mappings (lambda:ListEventSourceMappings:
which function reads which queue, stream or topic).

Only names are kept: every ARN becomes the resource's name, a state machine's definition is read for its integrations
and dropped (its parameters can hold endpoints and headers), and self-managed Kafka brokers are never stored."""
from __future__ import annotations

import json
import re
from typing import Any

from aws_inventory import mask_text
from awsutil import metric_totals, paginate
from iac_common import mask_numbers

MAX_STATES, MAX_RULES, MAX_DEPTH = 400, 200, 8
PARTITION = re.compile(r"aws(-[a-z]+)*")  # aws, aws-cn, aws-us-gov, …
STATES_API = re.compile(r"^arn:aws(?:-[a-z]+)*:states:::")
MANAGED_RULES = ("AmazonCloudWatch", "AutoScaling", "EKS")
SERVICE_KIND = {"lambda": "lambda", "sqs": "sqs", "sns": "sns", "states": "sfn", "kinesis": "kinesis", "dynamodb": "dynamodb", "kafka": "msk",
                "ecs": "ecs", "events": "eventbridge", "firehose": "firehose", "logs": "logs"}


def arn_ref(arn: Any) -> tuple[str, str]:
    """(kind, name) of an ARN, or of a queue URL; ("", "") for anything else."""
    s = str(arn or "")
    if s.startswith("https://sqs."):
        return "sqs", s.rstrip("/").rsplit("/", 1)[-1]
    p = s.split(":", 5)
    if len(p) < 6 or p[0] != "arn" or not PARTITION.fullmatch(p[1]):
        return "", ""
    service, res = p[2], p[5]
    kind = SERVICE_KIND.get(service, service)
    if service == "lambda":
        name = res.split(":")[1] if res.startswith("function:") else res
    elif service == "states":
        name = res.split(":")[1] if ":" in res else res
    elif service in ("kinesis", "kafka", "dynamodb", "events", "firehose"):
        name = res.split("/")[1] if "/" in res else res
    elif service == "ecs":
        name = res.split("/")[-1].split(":")[0]
    else:
        name = res.split("/")[-1].split(":")[-1]
    return kind, mask_numbers(name)[:120]


def _param(params: Any, *keys: str) -> Any:
    """A literal parameter value; None for JSONPath ($.x), intrinsic or JSONata ({% … %}) values, worked out at run time."""
    if not isinstance(params, dict):
        return None
    v = next((params[k] for k in keys if isinstance(params.get(k), str)), None)
    return None if v is None or v.startswith(("$", "{%")) or "States." in v else v


def _integration(resource: str, params: Any) -> dict | None:
    if re.match(r"^arn:aws(?:-[a-z]+)*:lambda:", resource):
        kind, name = arn_ref(resource)
        return {"kind": kind, "n": name}
    m = STATES_API.match(resource)
    if not m:
        return None
    api = resource[m.end():].split(".")[0]
    service = api.split(":")[0]
    if service == "lambda":
        fn = _param(params, "FunctionName")
        return {"kind": "lambda", "n": arn_ref(fn)[1] if str(fn).startswith("arn:") else mask_numbers(str(fn).split(":")[0])[:120]} if fn else None
    if service in ("sqs", "sns", "states", "ecs", "dynamodb"):
        ref = _param(params, {"sqs": "QueueUrl", "sns": "TopicArn", "states": "StateMachineArn", "ecs": "TaskDefinition", "dynamodb": "TableName"}[service])
        if not ref:
            return None
        if service == "dynamodb":
            return {"kind": "dynamodb", "n": mask_numbers(ref)[:120]}
        kind, name = arn_ref(ref)
        return {"kind": kind or SERVICE_KIND[service], "n": name or mask_numbers(ref.split("/")[-1].split(":")[0])[:120]}
    if service == "http":
        return {"kind": "http", "n": "HTTP endpoint"}
    if service == "events":
        return {"kind": "eventbridge", "n": "PutEvents"}
    if service == "aws-sdk":
        return {"kind": "aws", "n": ":".join(api.split(":")[1:3])[:80]}
    return {"kind": SERVICE_KIND.get(service, service)[:30], "n": api[:80]}


def asl_targets(definition: str) -> list[dict]:
    """The services a state machine calls, in the order its states are written (Parallel branches and Map processors included)."""
    try:
        doc = json.loads(definition)
    except (TypeError, ValueError):
        return []
    out: list[dict] = []
    budget = [MAX_STATES]

    def walk(states: Any, depth: int) -> None:
        if not isinstance(states, dict) or depth > MAX_DEPTH:
            return
        for st in states.values():
            budget[0] -= 1
            if budget[0] < 0 or not isinstance(st, dict):
                return
            if st.get("Type") == "Task" and isinstance(st.get("Resource"), str):
                hit = _integration(st["Resource"], st.get("Parameters") or st.get("Arguments"))
                if hit and hit not in out:
                    out.append(hit)
            for branch in st.get("Branches") or []:
                walk(branch.get("States") if isinstance(branch, dict) else None, depth + 1)
            for key in ("Iterator", "ItemProcessor"):
                if isinstance(st.get(key), dict):
                    walk(st[key].get("States"), depth + 1)

    walk(doc.get("States") if isinstance(doc, dict) else None, 0)
    return out


def collect_sfn(s: Any, cw: Any) -> list[dict]:
    sf = s.client("stepfunctions")
    machines = paginate(sf, "list_state_machines", "stateMachines")
    rows = [{"n": m["name"], "type": m.get("type", ""), "targets": asl_targets(sf.describe_state_machine(stateMachineArn=m["stateMachineArn"]).get("definition", ""))}
            for m in machines]
    q = []
    for i, m in enumerate(machines):
        d = {"StateMachineArn": m["stateMachineArn"]}
        q += [(f"f{i}", "AWS/States", "ExecutionsFailed", d, "Sum"), (f"s{i}", "AWS/States", "ExecutionsStarted", d, "Sum")]
    totals = metric_totals(cw, q)
    return [{**r, "failed": totals.get(f"f{i}", 0), "started": totals.get(f"s{i}", 0)} for i, r in enumerate(rows)]


def collect_kinesis(s: Any, cw: Any) -> list[dict]:
    k = s.client("kinesis")
    names = paginate(k, "list_streams", "StreamNames")
    rows = []
    for n in names:
        d = k.describe_stream_summary(StreamName=n)["StreamDescriptionSummary"]
        rows.append({"n": n, "status": d.get("StreamStatus", ""), "mode": (d.get("StreamModeDetails") or {}).get("StreamMode", ""), "shards": d.get("OpenShardCount", 0)})
    q = []
    for i, n in enumerate(names):
        q += [(f"r{i}", "AWS/Kinesis", "IncomingRecords", {"StreamName": n}, "Sum"), (f"a{i}", "AWS/Kinesis", "GetRecords.IteratorAgeMilliseconds", {"StreamName": n}, "Maximum")]
    totals = metric_totals(cw, q)
    return [{**r, "incoming": totals.get(f"r{i}", 0), "iteratorAgeMs": totals.get(f"a{i}", 0)} for i, r in enumerate(rows)]


def collect_msk(s: Any) -> list[dict]:
    out = []
    for c in paginate(s.client("kafka"), "list_clusters_v2", "ClusterInfoList"):
        p = c.get("Provisioned") or {}
        out.append({"n": c.get("ClusterName", ""), "type": c.get("ClusterType", ""), "state": c.get("State", ""), "brokers": p.get("NumberOfBrokerNodes", 0),
                    "instance": (p.get("BrokerNodeGroupInfo") or {}).get("InstanceType", ""), "version": (p.get("CurrentBrokerSoftwareInfo") or {}).get("KafkaVersion", "")})
    return out


def collect_eb_targets(s: Any) -> list[dict]:
    ev = s.client("events")
    out = []
    rules = [r["Name"] for r in paginate(ev, "list_rules", "Rules") if not r["Name"].startswith(MANAGED_RULES)][:MAX_RULES]
    for rule in rules:
        for t in ev.list_targets_by_rule(Rule=rule).get("Targets", []):
            kind, name = arn_ref(t.get("Arn"))
            if kind:
                out.append({"rule": rule, "kind": kind, "n": name})
    return out


def collect_esm(s: Any) -> list[dict]:
    out = []
    for m in paginate(s.client("lambda"), "list_event_source_mappings", "EventSourceMappings"):
        kind, source = arn_ref(m.get("EventSourceArn"))
        topics = [str(t)[:120] for t in m.get("Topics") or []]
        if not kind and m.get("SelfManagedEventSource"):
            kind, source = "kafka", topics[0] if topics else "self-managed Kafka"  # broker addresses are not kept
        out.append({"fn": arn_ref(m.get("FunctionArn"))[1], "kind": kind, "source": source, "topics": topics, "state": str(m.get("State") or ""),
                    "last": mask_text(m.get("LastProcessingResult") or "", 160)})
    return out


def add_to(out: dict, s: Any, cw: Any, safe: Any) -> None:
    """Each collector on its own: one that lacks permission is skipped with a warning and leaves an empty list."""
    out["sfn"] = safe("step functions", collect_sfn, s, cw) or []
    out["kinesis"] = safe("kinesis", collect_kinesis, s, cw) or []
    out["msk"] = safe("msk", collect_msk, s) or []
    out["ebTargets"] = safe("eventbridge targets", collect_eb_targets, s) or []
    out["esm"] = safe("lambda event source mappings", collect_esm, s) or []
    for k in ("sfn", "kinesis", "msk", "ebTargets", "esm"):
        out[k] = mask_numbers(out[k])
