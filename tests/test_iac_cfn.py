# tests/test_iac_cfn.py
import iac_cfn

TEMPLATE = """
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31
Resources:
  Cluster:
    Type: AWS::ECS::Cluster
    Properties: {ClusterName: prod-kestrel}
  ApiTask:
    Type: AWS::ECS::TaskDefinition
    Properties:
      ContainerDefinitions:
        - Name: shop-api
          Image: !Sub "${AWS::AccountId}.dkr.ecr.${AWS::Region}.amazonaws.com/shop-api:latest"
          Environment:
            - {Name: ORDERS_QUEUE, Value: kestrel-orders.fifo}
  ApiService:
    Type: AWS::ECS::Service
    Properties:
      ServiceName: shop-api
      Cluster: !Ref Cluster
      TaskDefinition: !Ref ApiTask
      DesiredCount: 2
      LoadBalancers: [{TargetGroupArn: !Ref ApiTg, ContainerName: shop-api}]
  ApiTg:
    Type: AWS::ElasticLoadBalancingV2::TargetGroup
    Properties: {Name: shop-api}
  Alb:
    Type: AWS::ElasticLoadBalancingV2::LoadBalancer
    Properties: {Name: kestrel-public, Scheme: internet-facing}
  Https:
    Type: AWS::ElasticLoadBalancingV2::Listener
    Properties:
      LoadBalancerArn: !Ref Alb
      Port: 443
      DefaultActions: [{Type: forward, TargetGroupArn: !Ref ApiTg}]
  OrdersRule:
    Type: AWS::ElasticLoadBalancingV2::ListenerRule
    Properties:
      ListenerArn: !Ref Https
      Conditions: [{Field: path-pattern, Values: ["/orders/*"]}]
      Actions: [{Type: forward, TargetGroupArn: !Ref ApiTg}]
  Orders:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: kestrel-orders.fifo
      RedrivePolicy: {deadLetterTargetArn: !GetAtt OrdersDlq.Arn, maxReceiveCount: 5}
  OrdersDlq:
    Type: AWS::SQS::Queue
    Properties: {QueueName: kestrel-orders-dlq.fifo}
  Resize:
    Type: AWS::Serverless::Function
    Properties: {FunctionName: image-resize, MemorySize: 1024, CodeUri: functions/resize}
  Members:
    Type: AWS::Cognito::UserPool
    Properties: {UserPoolName: Kestrel Members}
"""


def test_ecs_service_from_template() -> None:
    (svc,) = iac_cfn.read(TEMPLATE, "template.yaml")["ecs"]
    assert (svc["cluster"], svc["n"], svc["desired"], svc["ownImage"]) == ("prod-kestrel", "shop-api", 2, True)
    assert {"k": "ORDERS_QUEUE", "v": "kestrel-orders.fifo"} in svc["refs"]


def test_routes_and_named_resources() -> None:
    part = iac_cfn.read(TEMPLATE, "template.yaml")
    whens = {r["when"]: r["services"] for r in part["lb"][0]["routes"]}
    assert whens == {"path-pattern=/orders/*": ["prod-kestrel/shop-api"], "default": ["prod-kestrel/shop-api"]}
    q = {x["n"]: x for x in part["sqs"]}
    assert q["kestrel-orders.fifo"]["redrive"] is True and q["kestrel-orders-dlq.fifo"]["dlq"] is True
    assert part["lambda"] == [{"n": "image-resize", "mem": 1024, "inv": 0, "err": 0.0, "dur": 0.0, "fam": "image-resize"}]
    assert part["cognito"] == [{"n": "Kestrel Members"}]


def test_json_template_and_non_templates() -> None:
    assert iac_cfn.read('{"Resources": {"T": {"Type": "AWS::SNS::Topic", "Properties": {"TopicName": "a"}}}}', "t.json")["sns"][0]["n"] == "a"
    assert iac_cfn.read("services: {}\n", "x.yaml")["ecs"] == []


ODD = """
Resources:
  Cluster: {Type: AWS::ECS::Cluster, Properties: {ClusterName: c}}
  Task:
    Type: AWS::ECS::TaskDefinition
    Properties:
      ContainerDefinitions:
        - {Name: a, Image: !Ref ImageParam}
        - {Name: b, Image: !Join ["", ["x", "y"]]}
        - {Name: c, Image: 5}
  Svc:
    Type: AWS::ECS::Service
    Properties: {ServiceName: s, Cluster: !Ref Cluster, TaskDefinition: !Ref Task, DesiredCount: !Ref Count}
  Fn: {Type: AWS::Lambda::Function, Properties: {FunctionName: f, MemorySize: !Ref Mem}}
  Bad: {Type: AWS::SQS::Queue, Properties: broken}
  Alb: {Type: AWS::ElasticLoadBalancingV2::LoadBalancer, Properties: {Name: lb}}
  L: {Type: AWS::ElasticLoadBalancingV2::Listener, Properties: {LoadBalancerArn: !Ref Alb, Port: !Ref P, DefaultActions: [oops, {TargetGroupArn: nope}]}}
  R: {Type: AWS::ElasticLoadBalancingV2::ListenerRule, Properties: {ListenerArn: !Ref L, Conditions: [oops, 3], Actions: oops}}
"""


def test_unreadable_values_fall_back_instead_of_crashing() -> None:
    part = iac_cfn.read(ODD, "t.yaml")
    (svc,) = part["ecs"]
    assert svc["desired"] == 1 and svc["images"] == [] and svc["ownImage"] is True
    assert part["lambda"][0]["mem"] == 128 and part["lb"][0]["routes"] == []


def test_target_group_type_and_targets() -> None:
    import json
    t = {"Resources": {"LB": {"Type": "AWS::ElasticLoadBalancingV2::LoadBalancer", "Properties": {"Name": "pub"}},
                       "TG": {"Type": "AWS::ElasticLoadBalancingV2::TargetGroup", "Properties": {"Name": "fn", "TargetType": "lambda", "Targets": [{"Id": "x"}]}},
                       "L": {"Type": "AWS::ElasticLoadBalancingV2::Listener", "Properties": {"LoadBalancerArn": {"Ref": "LB"}, "Port": 443,
                             "DefaultActions": [{"Type": "forward", "TargetGroupArn": {"Ref": "TG"}}]}}}}
    r = iac_cfn.read(json.dumps(t), "t.json")["lb"][0]["routes"][0]
    assert r["tgType"] == "lambda" and r["attached"] is True


def test_hostile_cfn_engine_values_are_dropped() -> None:
    import json
    t = {"Resources": {"D": {"Type": "AWS::RDS::DBInstance", "Properties": {"Engine": "<img src=x onerror=alert(1)>"}},
                       "C": {"Type": "AWS::ElastiCache::CacheCluster", "Properties": {"Engine": "<svg onload=alert(1)>"}}}}
    part = iac_cfn.read(json.dumps(t), "t.json")
    assert part["rds"][0]["engine"] == "" and part["cache"][0]["engine"] == ""
