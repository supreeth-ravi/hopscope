"""Step Functions, Kinesis, MSK, EventBridge targets and Lambda event source mappings (aws_streams.py), and the rules
over them (rules_streams.py). Stubbed boto3 only: no AWS account is used."""
from __future__ import annotations

import json

import aws_streams
import rules_streams

ACCT = "0" * 11 + "9"
R = "eu-west-1"


def arn(service: str, res: str) -> str:
    return f"arn:aws:{service}:{R}:{ACCT}:{res}"


DEFINITION = {
    "StartAt": "Charge",
    "States": {
        "Charge": {"Type": "Task", "Resource": "arn:aws:states:::lambda:invoke", "Parameters": {"FunctionName": arn("lambda", "function:kestrel-charge:live"), "Payload.$": "$"}, "Next": "Fan"},
        "Fan": {"Type": "Parallel", "Next": "Each", "Branches": [
            {"StartAt": "Notify", "States": {"Notify": {"Type": "Task", "Resource": "arn:aws:states:::sns:publish", "Parameters": {"TopicArn": arn("sns", "kestrel-receipts"), "Message.$": "$"}, "End": True}}},
            {"StartAt": "Queue", "States": {"Queue": {"Type": "Task", "Resource": "arn:aws:states:::sqs:sendMessage.waitForTaskToken",
                                                      "Parameters": {"QueueUrl": f"https://sqs.{R}.amazonaws.com/{ACCT}/kestrel-roast-jobs", "MessageBody": {"t.$": "$$.Task.Token"}}, "End": True}}},
        ]},
        "Each": {"Type": "Map", "Next": "Save", "ItemProcessor": {"StartAt": "Grind", "States": {
            "Grind": {"Type": "Task", "Resource": arn("lambda", "function:kestrel-grind"), "End": True}}}},
        "Save": {"Type": "Task", "Resource": "arn:aws:states:::dynamodb:putItem", "Parameters": {"TableName": "kestrel-orders", "Item": {}}, "Next": "Roast"},
        "Roast": {"Type": "Task", "Resource": "arn:aws:states:::ecs:runTask.sync", "Parameters": {"TaskDefinition": arn("ecs", "task-definition/roaster:12"), "Cluster": "prod"}, "Next": "Child"},
        "Child": {"Type": "Task", "Resource": "arn:aws:states:::states:startExecution.sync:2", "Parameters": {"StateMachineArn": arn("states", "stateMachine:kestrel-refund")}, "Next": "Dyn"},
        "Dyn": {"Type": "Task", "Resource": "arn:aws:states:::lambda:invoke", "Parameters": {"FunctionName.$": "$.fn"}, "Next": "Http"},
        "Http": {"Type": "Task", "Resource": "arn:aws:states:::http:invoke", "Parameters": {"ApiEndpoint": "https://pay.example/x", "Headers": {"Authorization": "Bearer s3cr3t"}}, "End": True},
    },
}


def test_asl_integrations_in_order_without_secrets() -> None:
    got = aws_streams.asl_targets(json.dumps(DEFINITION))
    assert got == [{"kind": "lambda", "n": "kestrel-charge"}, {"kind": "sns", "n": "kestrel-receipts"}, {"kind": "sqs", "n": "kestrel-roast-jobs"},
                   {"kind": "lambda", "n": "kestrel-grind"}, {"kind": "dynamodb", "n": "kestrel-orders"}, {"kind": "ecs", "n": "roaster"},
                   {"kind": "sfn", "n": "kestrel-refund"}, {"kind": "http", "n": "HTTP endpoint"}]
    assert "s3cr3t" not in json.dumps(got) and ACCT not in json.dumps(got)
    assert aws_streams.asl_targets("not json") == [] and aws_streams.asl_targets('{"States": 3}') == []


class Pager:
    def __init__(self, pages: dict):
        self.pages = pages

    def paginate(self, **kw):
        yield self.pages


class Client:
    def __init__(self, name: str):
        self.name = name

    def get_paginator(self, op):
        return Pager({
            "list_state_machines": {"stateMachines": [{"name": "kestrel-checkout", "stateMachineArn": arn("states", "stateMachine:kestrel-checkout"), "type": "STANDARD"}]},
            "list_streams": {"StreamNames": ["kestrel-clicks"]},
            "list_clusters_v2": {"ClusterInfoList": [{"ClusterName": "kestrel-events", "ClusterType": "PROVISIONED", "State": "ACTIVE", "ClusterArn": arn("kafka", "cluster/kestrel-events/abc-1"),
                                                      "Provisioned": {"NumberOfBrokerNodes": 3, "BrokerNodeGroupInfo": {"InstanceType": "kafka.m5.large"},
                                                                      "CurrentBrokerSoftwareInfo": {"KafkaVersion": "3.6.0"}}}]},
            "list_rules": {"Rules": [{"Name": "kestrel-nightly"}, {"Name": "AutoScalingManagedRule"}]},
            "list_event_source_mappings": {"EventSourceMappings": [
                {"FunctionArn": arn("lambda", "function:kestrel-clicks-reader"), "EventSourceArn": arn("kinesis", "stream/kestrel-clicks"), "State": "Enabled",
                 "LastProcessingResult": "PROBLEM: Function call failed"},
                {"FunctionArn": arn("lambda", "function:kestrel-mailer"), "EventSourceArn": arn("sqs", "kestrel-emails"), "State": "Disabled", "LastProcessingResult": "OK"},
                {"FunctionArn": arn("lambda", "function:kestrel-sync"), "EventSourceArn": arn("dynamodb", "table/kestrel-orders/stream/2026-01-01T00:00:00.000"), "State": "Enabled"},
                {"FunctionArn": arn("lambda", "function:kestrel-ledger"), "EventSourceArn": arn("kafka", "cluster/kestrel-events/abc-1"), "Topics": ["orders"], "State": "Enabled"},
                {"FunctionArn": arn("lambda", "function:kestrel-legacy"), "SelfManagedEventSource": {"Endpoints": {"KAFKA_BOOTSTRAP_SERVERS": ["10.0.0.1:9092"]}},
                 "Topics": ["legacy-orders"], "State": "Enabled"},
            ]},
        }[op])

    def describe_state_machine(self, stateMachineArn):
        return {"definition": json.dumps(DEFINITION), "roleArn": arn("iam", "role/x")}

    def describe_stream_summary(self, StreamName):
        return {"StreamDescriptionSummary": {"StreamName": StreamName, "StreamStatus": "ACTIVE", "OpenShardCount": 4, "RetentionPeriodHours": 24,
                                             "StreamModeDetails": {"StreamMode": "PROVISIONED"}, "StreamARN": arn("kinesis", "stream/kestrel-clicks")}}

    def list_targets_by_rule(self, Rule):
        assert Rule == "kestrel-nightly"
        return {"Targets": [{"Id": "1", "Arn": arn("states", "stateMachine:kestrel-checkout")}, {"Id": "2", "Arn": arn("lambda", "function:kestrel-report")},
                            {"Id": "3", "Arn": arn("sqs", "kestrel-roast-jobs")}]}


class CW:
    def get_metric_data(self, **kw):
        out = []
        for q in kw["MetricDataQueries"]:
            name = q["MetricStat"]["Metric"]["MetricName"]
            out.append({"Id": q["Id"], "Values": {"ExecutionsFailed": [7], "ExecutionsStarted": [120], "IncomingRecords": [5000],
                                                  "GetRecords.IteratorAgeMilliseconds": [7_200_000]}.get(name, [])})
        return {"MetricDataResults": out}


class Session:
    def client(self, name, **kw):
        return Client(name)


def _collect() -> dict:
    out: dict = {}
    aws_streams.add_to(out, Session(), CW(), lambda name, fn, *a: fn(*a))
    return out


def test_collectors_shape_and_masking() -> None:
    out = _collect()
    assert out["sfn"] == [{"n": "kestrel-checkout", "type": "STANDARD", "targets": aws_streams.asl_targets(json.dumps(DEFINITION)), "failed": 7, "started": 120}]
    assert out["kinesis"] == [{"n": "kestrel-clicks", "status": "ACTIVE", "mode": "PROVISIONED", "shards": 4, "incoming": 5000, "iteratorAgeMs": 7_200_000}]
    assert out["msk"] == [{"n": "kestrel-events", "type": "PROVISIONED", "state": "ACTIVE", "brokers": 3, "instance": "kafka.m5.large", "version": "3.6.0"}]
    assert out["ebTargets"] == [{"rule": "kestrel-nightly", "kind": "sfn", "n": "kestrel-checkout"}, {"rule": "kestrel-nightly", "kind": "lambda", "n": "kestrel-report"},
                                {"rule": "kestrel-nightly", "kind": "sqs", "n": "kestrel-roast-jobs"}]
    esm = {e["fn"]: e for e in out["esm"]}
    assert (esm["kestrel-clicks-reader"]["kind"], esm["kestrel-clicks-reader"]["source"]) == ("kinesis", "kestrel-clicks")
    assert (esm["kestrel-mailer"]["kind"], esm["kestrel-mailer"]["source"], esm["kestrel-mailer"]["state"]) == ("sqs", "kestrel-emails", "Disabled")
    assert (esm["kestrel-sync"]["kind"], esm["kestrel-sync"]["source"]) == ("dynamodb", "kestrel-orders")
    assert (esm["kestrel-ledger"]["kind"], esm["kestrel-ledger"]["source"], esm["kestrel-ledger"]["topics"]) == ("msk", "kestrel-events", ["orders"])
    assert (esm["kestrel-legacy"]["kind"], esm["kestrel-legacy"]["source"]) == ("kafka", "legacy-orders")
    text = json.dumps(out)
    assert ACCT not in text and "arn:" not in text and "10.0.0.1" not in text and "abc-1" not in text


def test_a_collector_that_fails_is_skipped_alone(capsys) -> None:
    import aws_inventory

    class Broken(Session):
        def client(self, name, **kw):
            if name == "kinesis":
                raise PermissionError("AccessDenied")
            return Client(name)

    out: dict = {}
    aws_streams.add_to(out, Broken(), CW(), aws_inventory.safe)
    assert out["kinesis"] == [] and out["sfn"] and "kinesis skipped" in capsys.readouterr().out


def test_rules_for_failed_executions_old_records_and_mappings() -> None:
    acts = rules_streams.actions(_collect(), rules_streams.THRESHOLDS)
    titles = [a[1] for a in acts]
    assert "kestrel-checkout: 7 Step Functions executions failed in 3 days" in titles
    assert "Readers of Kinesis stream kestrel-clicks are 2.0 hours behind" in titles
    assert "kestrel-clicks-reader can't process kestrel-clicks" in titles
    assert not any("disabled" in t for t in titles)  # kestrel-emails has no traffic numbers here: a quietly paused mapping is not an action
    assert [a[0] for a in acts] == sorted((a[0] for a in acts), key=lambda x: {"bad": 0, "warn": 1}[x])
    quiet = {"sfn": [{"n": "a", "failed": 0, "started": 3}], "kinesis": [{"n": "b", "iteratorAgeMs": 500}], "esm": [{"fn": "c", "source": "d", "state": "Enabled", "last": "OK"}]}
    assert rules_streams.actions(quiet, rules_streams.THRESHOLDS) == []
    assert rules_streams.actions({}, rules_streams.THRESHOLDS) == []


def test_stream_actions_reach_the_model(tmp_path) -> None:
    import rules

    inv = {**_collect(), "ecs": [], "collectedAt": "2026-11-10T12:00:00+00:00"}
    first, _ = rules.extra_actions(tmp_path, inv, {}, False, rules.THRESHOLDS)
    assert any("Step Functions executions failed" in a[1] for a in first)
    first, _ = rules.extra_actions(tmp_path, {**inv, "mode": "design"}, {}, True, rules.THRESHOLDS)
    assert first == []


def test_changes_cover_the_new_resources() -> None:
    import history

    old = {"sfn": [{"n": "kestrel-checkout", "type": "STANDARD"}], "kinesis": [{"n": "kestrel-clicks", "shards": 2}], "eks": [{"n": "kestrel-prod", "version": "1.30"}]}
    new = {"sfn": [], "kinesis": [{"n": "kestrel-clicks", "shards": 4}], "msk": [{"n": "kestrel-events", "brokers": 3}], "eks": [{"n": "kestrel-prod", "version": "1.31"}]}
    d = history.diff_resources(history.resources(old), history.resources(new))
    assert {(x["kind"], x["name"]) for x in d["removed"]} == {("sfn", "kestrel-checkout")} and {(x["kind"], x["name"]) for x in d["added"]} == {("msk", "kestrel-events")}
    assert {(x["kind"], x["field"], x["to"]) for x in d["changed"]} == {("kinesis", "shards", 4), ("eks", "version", "1.31")}


def test_disabled_event_sources_are_one_action_and_only_when_messages_wait() -> None:
    esm = [{"fn": f"kestrel-fn-{i}", "kind": "sqs", "source": f"kestrel-q{i}", "state": "Disabled", "last": "OK"} for i in range(6)]
    esm.append({"fn": "kestrel-clicks-reader", "kind": "kinesis", "source": "kestrel-clicks", "state": "Disabled", "last": "OK"})
    inv = {"esm": esm, "sqs": [{"n": "kestrel-q0", "sent": 12, "backlog": 0}, {"n": "kestrel-q1", "sent": 0, "backlog": 3}] +
           [{"n": f"kestrel-q{i}", "sent": 0, "backlog": 0} for i in range(2, 6)], "kinesis": [{"n": "kestrel-clicks", "incoming": 900}]}
    acts = rules_streams.actions(inv, rules_streams.THRESHOLDS)
    assert len(acts) == 1 and acts[0][0] == "warn"
    assert acts[0][1] == "3 event sources are disabled while messages still arrive"
    assert "kestrel-fn-0 (kestrel-q0)" in acts[0][2] and "kestrel-clicks-reader (kestrel-clicks)" in acts[0][2] and "kestrel-fn-2" not in acts[0][2]
    assert "4 more are disabled with nothing waiting" in acts[0][2]
    quiet = {"esm": esm[2:6], "sqs": inv["sqs"]}
    assert rules_streams.actions(quiet, rules_streams.THRESHOLDS) == []


def test_asl_other_partitions_and_jsonata_values() -> None:
    gov = {"States": {
        "A": {"Type": "Task", "Resource": "arn:aws-us-gov:states:::lambda:invoke", "Parameters": {"FunctionName": f"arn:aws-us-gov:lambda:us-gov-west-1:{ACCT}:function:kestrel-gov"}},
        "B": {"Type": "Task", "Resource": f"arn:aws-cn:lambda:cn-north-1:{ACCT}:function:kestrel-cn"},
        "C": {"Type": "Task", "Resource": "arn:aws:states:::lambda:invoke", "Arguments": {"FunctionName": "{% $states.input.fn %}"}},
        "D": {"Type": "Task", "Resource": "arn:aws:states:::sqs:sendMessage", "Parameters": {"QueueUrl": "$.queue"}},
        "E": {"Type": "Task", "Resource": "arn:aws-cn:states:::sns:publish", "Parameters": {"TopicArn": f"arn:aws-cn:sns:cn-north-1:{ACCT}:kestrel-cn-topic"}}}}
    got = aws_streams.asl_targets(json.dumps(gov))
    assert got == [{"kind": "lambda", "n": "kestrel-gov"}, {"kind": "lambda", "n": "kestrel-cn"}, {"kind": "sns", "n": "kestrel-cn-topic"}]
