"""Code side of workflows and streams: Step Functions, Kinesis and Kafka calls in code (externals and call flows), the
links from code that publishes to a stream to the Lambdas that consume it, and traces that follow them. Kestrel Coffee
fixtures only."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import code_scan
import hopscope_query as q
import linker_streams
from langs import braceflow, csharp, java, js, python

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "hopscope" / "scripts"

APP = """import boto3
from fastapi import FastAPI
from kafka import KafkaProducer

app = FastAPI()
sfn = boto3.client("stepfunctions")
kinesis = boto3.client("kinesis")
producer = KafkaProducer()


@app.post("/orders")
def create_order():
    kinesis.put_record(StreamName="kestrel-clicks", Data=b"x", PartitionKey="1")
    sfn.start_execution(stateMachineArn=CHECKOUT_ARN, input="{}")
    producer.send("orders", b"x")
    return {}
"""


def test_externals_in_every_language() -> None:
    assert {"sfn", "kinesis", "kafka"} <= python.externals(APP)
    assert {"sfn", "kinesis"} <= java.externals("import software.amazon.awssdk.services.sfn.SfnClient;\nimport software.amazon.awssdk.services.kinesis.KinesisClient;\n")
    assert {"sfn", "kinesis"} <= csharp.externals("using Amazon.StepFunctions;\nusing Amazon.Kinesis;\nclass A { IAmazonKinesis k; }\n")
    assert {"sfn", "kinesis", "kafka"} <= js.externals("import { SFNClient } from '@aws-sdk/client-sfn';\nimport { KinesisClient } from '@aws-sdk/client-kinesis';\nimport { Kafka } from 'kafkajs';\n")


def test_python_flow_records_stream_workflow_and_kafka_steps(tmp_path: Path) -> None:
    (tmp_path / "orders").mkdir()
    (tmp_path / "orders" / "main.py").write_text(APP)
    (tmp_path / "orders" / "pyproject.toml").write_text("[project]\nname='orders'\n")
    cm = code_scan.scan([tmp_path])
    code = tmp_path / "code.json"
    code.write_text(json.dumps({"base": str(tmp_path), "files": cm.files, "routes": cm.routes, "services": cm.services}))
    subprocess.run([sys.executable, str(SCRIPTS / "call_flow.py"), str(code), str(tmp_path / "flows.json")], check=True, capture_output=True)
    steps = json.loads((tmp_path / "flows.json").read_text())["0"]
    assert [(s[1], s[2]) for s in steps] == [("stream", "Kinesis: PutRecord kestrel-clicks"), ("workflow", "Step Functions: StartExecution"),
                                            ("kafka", "Kafka: send to orders")]


def test_java_flow_records_step_functions_and_kinesis() -> None:
    assert braceflow.fk.aws_service("SfnClient") == "Step Functions" and braceflow.fk.aws_service("IAmazonKinesis") == "Kinesis"


INV = {"kinesis": [{"n": "kestrel-clicks"}], "sfn": [{"n": "kestrel-checkout", "targets": [{"kind": "lambda", "n": "kestrel-charge"}, {"kind": "sqs", "n": "kestrel-roast-jobs"}]}],
       "esm": [{"fn": "kestrel-clicks-reader", "kind": "kinesis", "source": "kestrel-clicks", "topics": [], "state": "Enabled"},
               {"fn": "kestrel-ledger", "kind": "msk", "source": "kestrel-events", "topics": ["orders"], "state": "Enabled"},
               {"fn": "kestrel-mailer", "kind": "sqs", "source": "kestrel-emails", "topics": [], "state": "Enabled"}],
       "ecs": [{"cluster": "prod", "n": "orders-api", "refs": [{"k": "CHECKOUT_STATE_MACHINE", "v": "kestrel-checkout"}]}]}


def test_stream_links_from_code_and_container_settings(tmp_path: Path) -> None:
    (tmp_path / "orders").mkdir()
    (tmp_path / "orders" / "main.py").write_text(APP)
    code = {"base": str(tmp_path), "files": [{"p": "orders/main.py", "x": ["kinesis", "sfn", "kafka"]}], "services": ["orders"]}
    placement = {"prod/orders-api": {"code": "orders", "confidence": 1.0}}
    streams, consumers = linker_streams.stream_links(code, INV, ["orders"], placement, tmp_path)
    assert streams == {"orders": ["kafka:orders", "kinesis:kestrel-clicks", "sfn:kestrel-checkout"]}
    assert consumers == {"kinesis:kestrel-clicks": ["kestrel-clicks-reader"], "kafka:orders": ["kestrel-ledger"], "sqs:kestrel-emails": ["kestrel-mailer"]}


def test_stream_links_need_a_real_reference(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "x.py").write_text("# clicks are counted elsewhere\nname = 'kestrel-clicks-report'\n")
    code = {"base": str(tmp_path), "files": [{"p": "web/x.py", "x": ["kinesis"]}], "services": ["web"]}
    assert linker_streams.stream_links(code, INV, ["web"], {}, tmp_path)[0] == {}


def _data() -> dict:
    code = {"files": [{"p": "orders/main.py", "x": []}], "services": ["orders"], "routes": [{"m": "POST", "p": "/orders", "f": 0, "l": 12, "t": ""}]}
    flows = {"0": [[0, "stream", "Kinesis: PutRecord kestrel-clicks", 0, 13, 0], [0, "workflow", "Step Functions: StartExecution", 0, 14, 0],
                   [0, "queue", "SQS: SendMessage", 0, 15, 0]]}
    links = {"placement": {}, "streams": {"orders": ["kinesis:kestrel-clicks", "sfn:kestrel-checkout", "kinesis:kestrel-audit"]},
             "consumers": {"kinesis:kestrel-clicks": ["kestrel-clicks-reader"], "sqs:kestrel-emails": ["kestrel-mailer"]},
             "queues": {"orders": ["kestrel-emails"]}, "queueWorkers": {}}
    model = {"infra": [], "placement": {}, "queues": links["queues"], "queueWorkers": {}}
    return {"code": code, "flows": flows, "links": links, "model": model, "aws": INV, "paths": ["orders/main.py"], "changes": {}, "drift": {}}


def test_trace_follows_streams_workflows_and_lambda_consumers() -> None:
    t = q.trace(_data(), "POST /orders")
    assert t["streams"] == [
        {"kind": "kinesis", "name": "kestrel-clicks", "consumers": ["kestrel-clicks-reader"], "basis": "step"},
        {"kind": "sfn", "name": "kestrel-checkout", "consumers": [], "calls": ["lambda kestrel-charge", "sqs kestrel-roast-jobs"], "basis": "service publishes to"},
    ]
    assert t["queues"][0]["consumers"] == ["kestrel-mailer"]
    import hopscope_text
    text = hopscope_text.trace_text(t)
    assert "stream: kinesis kestrel-clicks -> kestrel-clicks-reader" in text and "workflow: kestrel-checkout calls lambda kestrel-charge" in text
