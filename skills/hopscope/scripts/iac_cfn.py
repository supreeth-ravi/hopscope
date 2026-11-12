# skills/hopscope/scripts/iac_cfn.py
"""CloudFormation and SAM templates (YAML or JSON) → design rows."""
from __future__ import annotations

import json
import re
from typing import Any

import yaml

import iac_common as c

FILE = re.compile(r"\.(ya?ml|json|template)$", re.I)
NAMED = {"AWS::SNS::Topic": ("sns", "TopicName"), "AWS::Cognito::UserPool": ("cognito", "UserPoolName"), "AWS::ApiGateway::RestApi": ("apigw", "Name"),
         "AWS::ApiGatewayV2::Api": ("apigw", "Name"), "AWS::Serverless::Api": ("apigw", "Name"), "AWS::Serverless::HttpApi": ("apigw", "Name"),
         "AWS::CloudFront::Distribution": ("cdn", ""), "AWS::DynamoDB::Table": ("dynamodb", "TableName"), "AWS::Serverless::SimpleTable": ("dynamodb", "TableName"),
         "AWS::Events::Rule": ("eventbridge", "Name"), "AWS::OpenSearchService::Domain": ("search", "DomainName"), "AWS::S3::Bucket": ("s3", "BucketName")}


class _Loader(yaml.SafeLoader):
    pass


def _tag(loader: yaml.Loader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        val: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        val = loader.construct_sequence(node, deep=True)
    else:
        val = loader.construct_mapping(node, deep=True)
    return {"Ref": val} if suffix == "Ref" else {f"Fn::{suffix}": val}


_Loader.add_multi_constructor("!", _tag)


def load(text: str) -> dict:
    try:
        data = json.loads(text) if text.lstrip().startswith("{") else yaml.load(text, Loader=_Loader)  # noqa: S506 - SafeLoader subclass
    except (yaml.YAMLError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("Resources"), dict):
        return {}
    return data["Resources"]


def _ref(v: Any) -> str | None:
    if isinstance(v, dict):
        if "Ref" in v:
            return str(v["Ref"])
        if "Fn::GetAtt" in v:
            target = v["Fn::GetAtt"]
            return str(target[0] if isinstance(target, list) else str(target).split(".")[0])
    return None


def _name(v: Any, fallback: str) -> str:
    return c.clean_name(v, fallback) if isinstance(v, (str, int)) else fallback


def _list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _image(v: Any) -> str:
    """An image name; "" (treated as our own) when it is built from a parameter, Join or import."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict) and isinstance(v.get("Fn::Sub"), (str, list)):
        sub = v["Fn::Sub"]
        first = sub[0] if isinstance(sub, list) and sub else sub
        return first if isinstance(first, str) else ""
    return ""


def read(text: str, rel: str) -> dict:
    res = load(text)
    part: dict = {"ecs": [], "lb": [], "sqs": [], "rds": [], "cache": [], "lambda": [], "sns": [], "cognito": [], "apigw": [], "cdn": [],
                  "dynamodb": [], "eventbridge": [], "search": [], "ses": [], "s3": {"count": 0}}
    typed = {lid: (r.get("Type", ""), r["Properties"] if isinstance(r.get("Properties"), dict) else {}) for lid, r in res.items() if isinstance(r, dict)}
    clusters = {lid: _name(p.get("ClusterName"), lid) for lid, (t, p) in typed.items() if t == "AWS::ECS::Cluster"}
    tg_names = {lid: _name(p.get("Name"), lid) for lid, (t, p) in typed.items() if t == "AWS::ElasticLoadBalancingV2::TargetGroup"}
    tg_props = {lid: p for lid, (t, p) in typed.items() if t == "AWS::ElasticLoadBalancingV2::TargetGroup"}
    tg_services: dict[str, list[str]] = {}
    for lid, (t, p) in typed.items():
        if t == "AWS::ECS::Service":
            name, cl = _name(p.get("ServiceName"), lid), clusters.get(_ref(p.get("Cluster")) or "", "cloudformation")
            td = typed.get(_ref(p.get("TaskDefinition")) or "", ("", {}))[1]
            defs = [d for d in _list(td.get("ContainerDefinitions")) if isinstance(d, dict)]
            env = {str(e["Name"]): e.get("Value", "") for d in defs for e in _list(d.get("Environment")) if isinstance(e, dict) and "Name" in e}
            tgs = [g for lb in _list(p.get("LoadBalancers")) if isinstance(lb, dict) and (g := _ref(lb.get("TargetGroupArn")))]
            part["ecs"].append(c.ecs_row(cl, name, desired=c.as_int(p.get("DesiredCount", 1), 1), images=[_image(d.get("Image")) for d in defs],
                                         env_vars=env, build_from=rel, source="cloudformation", tgs=[tg_names.get(g, g) for g in tgs],
                                         desired_set=c.as_int(p.get("DesiredCount"), -1) >= 0, family=_name(td.get("Family"), "")))
            for g in tgs:
                tg_services.setdefault(g, []).append(f"{cl}/{name}")
        elif t == "AWS::SQS::Queue":
            part["sqs"].append(c.sqs_row(_name(p.get("QueueName"), lid), redrive="RedrivePolicy" in p))
        elif t in ("AWS::RDS::DBCluster", "AWS::RDS::DBInstance"):
            name = _name(p.get("DBClusterIdentifier") or p.get("DBInstanceIdentifier"), lid)
            part["rds"].append(c.rds_row(name, str(p.get("Engine", "")), serverless="ServerlessV2ScalingConfiguration" in p))
        elif t in ("AWS::ElastiCache::CacheCluster", "AWS::ElastiCache::ReplicationGroup", "AWS::ElastiCache::ServerlessCache"):
            name = _name(p.get("ClusterName") or p.get("ReplicationGroupId") or p.get("ServerlessCacheName"), lid)
            part["cache"].append(c.cache_row(name, str(p.get("Engine", "redis"))))
        elif t in ("AWS::Lambda::Function", "AWS::Serverless::Function"):
            part["lambda"].append(c.lambda_row(_name(p.get("FunctionName"), lid), c.as_int(p.get("MemorySize", 128), 128)))
        elif t in NAMED:
            kind, key = NAMED[t]
            c.add_named(part, kind, _name(p.get(key) if key else None, lid))
    lbs = {lid: c.lb_row(_name(p.get("Name"), lid), internal=p.get("Scheme") == "internal")
           for lid, (t, p) in typed.items() if t == "AWS::ElasticLoadBalancingV2::LoadBalancer"}
    listeners = {lid: (_ref(p.get("LoadBalancerArn")), c.as_int(p.get("Port", 443), 443), p)
                 for lid, (t, p) in typed.items() if t == "AWS::ElasticLoadBalancingV2::Listener"}

    def add(lb_id: str | None, port: int, when: str, actions: list | None) -> None:
        for a in _list(actions):
            g = _ref(a.get("TargetGroupArn")) if isinstance(a, dict) else None
            if lb_id in lbs and g:
                tp = tg_props.get(g, {})
                lbs[lb_id]["routes"].append(c.route(when, tg_names.get(g, g), tg_services.get(g, []), port=port,
                                                    tg_type=str(tp.get("TargetType") or "ip").lower(), attached=bool(_list(tp.get("Targets")))))

    for _lid, (t, p) in typed.items():
        if t == "AWS::ElasticLoadBalancingV2::ListenerRule":
            lb_id, port, _ = listeners.get(_ref(p.get("ListenerArn")) or "", (None, 443, {}))
            conds = []
            for cond in _list(p.get("Conditions")):
                if not isinstance(cond, dict):
                    continue
                cfg = cond.get("PathPatternConfig") or cond.get("HostHeaderConfig")
                vals = cond.get("Values") or (cfg.get("Values") if isinstance(cfg, dict) else None)
                conds += [f'{cond.get("Field")}={v}' for v in _list(vals)]
            add(lb_id, port, " & ".join(conds) or "default", p.get("Actions"))
    for lb_id, port, p in listeners.values():
        add(lb_id, port, "default", p.get("DefaultActions"))
    part["lb"] = list(lbs.values())
    return part
