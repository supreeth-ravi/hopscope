"""Read-only inventory of one AWS account and region, with 3-day usage, written to aws.json.

    python aws_inventory.py --region ap-south-1 [--profile prod] --out data/aws.json

Every call is a Describe/List/Get. Container environment values are dropped unless they are
AWS endpoints or queue URLs (used to link code to queues and databases), so no secrets are stored.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from awsutil import SAFE_VALUE, env_of, family, metric_totals, paginate, session


def collect_sqs(s: Any, cw: Any) -> list[dict]:
    urls = paginate(s.client("sqs"), "list_queues", "QueueUrls")
    names = [u.rsplit("/", 1)[1] for u in urls]
    q = []
    for i, n in enumerate(names):
        d = {"QueueName": n}
        q += [(f"s{i}", "AWS/SQS", "NumberOfMessagesSent", d, "Sum"), (f"v{i}", "AWS/SQS", "ApproximateNumberOfMessagesVisible", d, "Maximum"),
              (f"a{i}", "AWS/SQS", "ApproximateAgeOfOldestMessage", d, "Maximum")]
    m = metric_totals(cw, q)
    return [{"n": n, "sent": m.get(f"s{i}", 0), "backlog": m.get(f"v{i}", 0), "age": m.get(f"a{i}", 0),
             "dlq": bool(re.search(r"dlq|dead", n, re.I)), "fifo": n.endswith(".fifo"), "fam": family(n)} for i, n in enumerate(names)]


def collect_lambda(s: Any, cw: Any) -> list[dict]:
    fns = paginate(s.client("lambda"), "list_functions", "Functions")
    q = []
    for i, f in enumerate(fns):
        d = {"FunctionName": f["FunctionName"]}
        q += [(f"i{i}", "AWS/Lambda", "Invocations", d, "Sum"), (f"e{i}", "AWS/Lambda", "Errors", d, "Sum"), (f"d{i}", "AWS/Lambda", "Duration", d, "Average")]
    m = metric_totals(cw, q)
    return [{"n": f["FunctionName"], "mem": f["MemorySize"], "inv": m.get(f"i{i}", 0), "err": m.get(f"e{i}", 0), "dur": m.get(f"d{i}", 0),
             "fam": family(f["FunctionName"])} for i, f in enumerate(fns)]


def collect_ecs(s: Any, cw: Any, env_rx: str | None) -> list[dict]:
    ecs = s.client("ecs")
    services = []
    for c in paginate(ecs, "list_clusters", "clusterArns"):
        cname = c.rsplit("/", 1)[1]
        arns = paginate(ecs, "list_services", "serviceArns", cluster=c)
        for i in range(0, len(arns), 10):
            for svc in ecs.describe_services(cluster=c, services=arns[i:i + 10])["services"]:
                td = ecs.describe_task_definition(taskDefinition=svc["taskDefinition"])["taskDefinition"]
                images, refs, names, own_image = [], [], [], False
                for ct in td.get("containerDefinitions", []):
                    names.append(ct["name"])
                    image = ct.get("image", "")
                    images.append(image.split("/")[-1].split(":")[0].split("@")[0])
                    own_image |= ".dkr.ecr." in image  # built by this account; public images are off-the-shelf software
                    # Keep only AWS endpoints and queue URLs, with the account number masked.
                    refs += [{"k": e["name"], "v": re.sub(r"\b\d{12}\b", "************", e["value"])} for e in ct.get("environment", []) if SAFE_VALUE.search(str(e.get("value", "")))]
                cpu, mem = int(td.get("cpu") or 0), int(td.get("memory") or 0)
                services.append({"cluster": cname, "n": svc["serviceName"], "running": svc["runningCount"], "desired": svc["desiredCount"],
                                 "size": f"{cpu / 1024:g} vCPU · {mem / 1024:g} GB" if cpu else "", "images": images, "ownImage": own_image, "containers": names, "refs": refs,
                                 "tgs": [lb["targetGroupArn"] for lb in svc.get("loadBalancers", []) if lb.get("targetGroupArn")],
                                 "env": env_of(f"{cname}/{svc['serviceName']}", env_rx)})
    q = []
    for i, x in enumerate(services):
        d = {"ClusterName": x["cluster"], "ServiceName": x["n"]}
        q += [(f"c{i}", "AWS/ECS", "CPUUtilization", d, "Average"), (f"m{i}", "AWS/ECS", "MemoryUtilization", d, "Average"),
              (f"mx{i}", "AWS/ECS", "MemoryUtilization", d, "Maximum"), (f"cx{i}", "AWS/ECS", "CPUUtilization", d, "Maximum")]
    m = metric_totals(cw, q)
    for i, x in enumerate(services):
        x.update(cpu=m.get(f"c{i}", 0), mem=m.get(f"m{i}", 0), memMax=m.get(f"mx{i}", 0), cpuMax=m.get(f"cx{i}", 0))
    return services


def collect_ec2(s: Any, cw: Any, env_rx: str | None) -> list[dict]:
    ec2 = s.client("ec2")
    inst = [i for r in paginate(ec2, "describe_instances", "Reservations") for i in r["Instances"] if i["State"]["Name"] in ("running", "stopped")]
    vols = {v["VolumeId"]: v["Size"] for v in paginate(ec2, "describe_volumes", "Volumes")}
    credit = {}
    burst = [i["InstanceId"] for i in inst if re.match(r"t\d", i["InstanceType"]) and i["State"]["Name"] == "running"]
    if burst:
        credit = {c["InstanceId"]: c["CpuCredits"] for c in ec2.describe_instance_credit_specifications(InstanceIds=burst)["InstanceCreditSpecifications"]}
    q = []
    for k, i in enumerate(inst):
        d = {"InstanceId": i["InstanceId"]}
        q += [(f"c{k}", "AWS/EC2", "CPUUtilization", d, "Average"), (f"cx{k}", "AWS/EC2", "CPUUtilization", d, "Maximum"),
              (f"b{k}", "AWS/EC2", "CPUCreditBalance", d, "Minimum"), (f"s{k}", "AWS/EC2", "CPUSurplusCreditsCharged", d, "Sum")]
    m = metric_totals(cw, q)
    out = []
    for k, i in enumerate(inst):
        name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), i["InstanceId"])
        out.append({"id": i["InstanceId"], "n": name, "type": i["InstanceType"], "state": i["State"]["Name"], "env": env_of(name, env_rx),
                    "cpu": m.get(f"c{k}", 0), "cpuMax": m.get(f"cx{k}", 0), "credits": credit.get(i["InstanceId"]),
                    "creditMin": m.get(f"b{k}") if i["InstanceId"] in credit else None, "surplus": m.get(f"s{k}", 0),
                    "diskGB": sum(vols.get(b["Ebs"]["VolumeId"], 0) for b in i.get("BlockDeviceMappings", []) if "Ebs" in b)})
    return out


def collect_rds(s: Any, cw: Any, env_rx: str | None) -> list[dict]:
    rds = s.client("rds")
    clusters = {c["DBClusterIdentifier"]: c for c in paginate(rds, "describe_db_clusters", "DBClusters")}
    dbs = paginate(rds, "describe_db_instances", "DBInstances")
    q = []
    for k, d in enumerate(dbs):
        dim = {"DBInstanceIdentifier": d["DBInstanceIdentifier"]}
        q += [(f"a{k}", "AWS/RDS", "ServerlessDatabaseCapacity", dim, "Average"), (f"ax{k}", "AWS/RDS", "ServerlessDatabaseCapacity", dim, "Maximum"),
              (f"c{k}", "AWS/RDS", "CPUUtilization", dim, "Average"), (f"n{k}", "AWS/RDS", "DatabaseConnections", dim, "Maximum")]
    m = metric_totals(cw, q)
    out = []
    for k, d in enumerate(dbs):
        cl = clusters.get(d.get("DBClusterIdentifier") or "", {})
        sv2 = cl.get("ServerlessV2ScalingConfiguration") or {}
        serverless = d["DBInstanceClass"] == "db.serverless"
        name = d.get("DBClusterIdentifier") or d["DBInstanceIdentifier"]
        out.append({"n": name, "instance": d["DBInstanceIdentifier"], "engine": d["Engine"], "class": d["DBInstanceClass"], "serverless": serverless,
                    "minAcu": sv2.get("MinCapacity"), "maxAcu": sv2.get("MaxCapacity"), "acu": m.get(f"a{k}", 0), "acuPeak": m.get(f"ax{k}", 0),
                    "cpu": m.get(f"c{k}", 0), "conns": m.get(f"n{k}", 0), "endpoint": (d.get("Endpoint") or {}).get("Address", ""),
                    "clusterEndpoint": cl.get("Endpoint", ""), "env": env_of(name, env_rx)})
    return out


def collect_lb(s: Any, cw: Any, ecs_services: list[dict]) -> list[dict]:
    elb = s.client("elbv2")
    tg_to_service: dict[str, list[str]] = {}
    for x in ecs_services:
        for tg in x["tgs"]:
            tg_to_service.setdefault(tg, []).append(f"{x['cluster']}/{x['n']}")
    lbs = paginate(elb, "describe_load_balancers", "LoadBalancers")
    q = []
    for i, lb in enumerate(lbs):
        dim = {"LoadBalancer": lb["LoadBalancerArn"].split(":loadbalancer/")[1]}
        if lb["Type"] == "application":
            q += [(f"r{i}", "AWS/ApplicationELB", "RequestCount", dim, "Sum"), (f"x{i}", "AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", dim, "Sum"),
                  (f"t{i}", "AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", dim, "Sum"), (f"e{i}", "AWS/ApplicationELB", "HTTPCode_ELB_503_Count", dim, "Sum")]
        else:
            q += [(f"r{i}", "AWS/NetworkELB", "NewFlowCount", dim, "Sum"), (f"x{i}", "AWS/NetworkELB", "TCP_Target_Reset_Count", dim, "Sum")]
    m = metric_totals(cw, q)
    out = []
    for i, lb in enumerate(lbs):
        health = {}
        for tg in elb.describe_target_groups(LoadBalancerArn=lb["LoadBalancerArn"])["TargetGroups"]:
            th = elb.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])["TargetHealthDescriptions"]
            health[tg["TargetGroupArn"]] = {"states": [t["TargetHealth"]["State"] for t in th], "instances": [t["Target"]["Id"] for t in th if t["Target"]["Id"].startswith("i-")]}
        routes = []
        for lis in elb.describe_listeners(LoadBalancerArn=lb["LoadBalancerArn"])["Listeners"]:
            rules = elb.describe_rules(ListenerArn=lis["ListenerArn"])["Rules"] if lb["Type"] == "application" else [{"Conditions": [], "Actions": lis["DefaultActions"]}]
            for r in rules:
                cond = []
                for c in r.get("Conditions", []):
                    vals = c.get("Values") or (c.get("HostHeaderConfig") or c.get("PathPatternConfig") or {}).get("Values", [])
                    cond.append(f'{c["Field"]}={",".join(vals)}')
                for a in r.get("Actions", []):
                    tgs = [a["TargetGroupArn"]] if a.get("TargetGroupArn") else [t["TargetGroupArn"] for t in (a.get("ForwardConfig") or {}).get("TargetGroups", [])]
                    for tg in tgs:
                        h = health.get(tg, {"states": [], "instances": []})
                        routes.append({"port": lis["Port"], "when": " & ".join(cond) or "default", "tg": tg.split("/")[-2], "services": tg_to_service.get(tg, []),
                                       "targets": h["states"], "instances": h["instances"]})
        out.append({"n": lb["LoadBalancerName"], "type": lb["Type"], "scheme": lb["Scheme"], "req": m.get(f"r{i}", 0), "err": m.get(f"x{i}", 0),
                    "e503": m.get(f"e{i}", 0), "t5xx": m.get(f"t{i}", 0), "routes": routes})
    return out


def collect_misc(s: Any, cw: Any) -> dict:
    out: dict[str, Any] = {}
    sns = [t["TopicArn"].rsplit(":", 1)[1] for t in paginate(s.client("sns"), "list_topics", "Topics")]
    m = metric_totals(cw, [(f"t{i}", "AWS/SNS", "NumberOfMessagesPublished", {"TopicName": t}, "Sum") for i, t in enumerate(sns)])
    out["sns"] = [{"n": t, "published": m.get(f"t{i}", 0)} for i, t in enumerate(sns)]
    ec = s.client("elasticache")
    nodes = paginate(ec, "describe_cache_clusters", "CacheClusters")
    q = []
    for i, c in enumerate(nodes):
        d = {"CacheClusterId": c["CacheClusterId"]}
        q += [(f"c{i}", "AWS/ElastiCache", "EngineCPUUtilization", d, "Average"), (f"m{i}", "AWS/ElastiCache", "DatabaseMemoryUsagePercentage", d, "Maximum"),
              (f"k{i}", "AWS/ElastiCache", "CurrConnections", d, "Maximum")]
    m = metric_totals(cw, q)
    out["cache"] = [{"n": c["CacheClusterId"], "type": c["CacheNodeType"], "engine": c["Engine"], "cpu": m.get(f"c{i}", 0), "mem": m.get(f"m{i}", 0),
                     "conns": m.get(f"k{i}", 0)} for i, c in enumerate(nodes)]
    osr = s.client("opensearch")
    acct = s.client("sts").get_caller_identity()["Account"]
    doms = [d["DomainName"] for d in osr.list_domain_names()["DomainNames"]]
    info = {d["DomainName"]: d for d in osr.describe_domains(DomainNames=doms)["DomainStatusList"]} if doms else {}
    q = []
    for i, d in enumerate(doms):
        dim = {"DomainName": d, "ClientId": acct}
        q += [(f"c{i}", "AWS/ES", "CPUUtilization", dim, "Average"), (f"f{i}", "AWS/ES", "FreeStorageSpace", dim, "Minimum")]
    m = metric_totals(cw, q)
    out["search"] = [{"n": d, "type": info[d]["ClusterConfig"]["InstanceType"], "count": info[d]["ClusterConfig"]["InstanceCount"],
                      "cpu": m.get(f"c{i}", 0), "freeGB": round(m.get(f"f{i}", 0) / 1024, 1)} for i, d in enumerate(doms)]
    apis = s.client("apigatewayv2").get_apis()["Items"]
    rest = paginate(s.client("apigateway"), "get_rest_apis", "items")
    m = metric_totals(cw, [(f"h{i}", "AWS/ApiGateway", "Count", {"ApiId": a["ApiId"]}, "Sum") for i, a in enumerate(apis)] +
                      [(f"r{i}", "AWS/ApiGateway", "Count", {"ApiName": a["name"]}, "Sum") for i, a in enumerate(rest)])
    out["apigw"] = [{"n": a["Name"], "proto": a["ProtocolType"], "req": m.get(f"h{i}", 0), "fam": family(a["Name"])} for i, a in enumerate(apis)] + \
                   [{"n": a["name"], "proto": "REST", "req": m.get(f"r{i}", 0), "fam": family(a["name"])} for i, a in enumerate(rest)]
    cf = s.client("cloudfront").list_distributions().get("DistributionList", {}).get("Items", [])
    out["cdn"] = [{"n": d.get("Comment") or d["Id"], "origin": d["Origins"]["Items"][0]["DomainName"].split(".")[0], "aliases": d.get("Aliases", {}).get("Items", [])} for d in cf]
    out["cognito"] = [{"n": p["Name"]} for p in s.client("cognito-idp").list_user_pools(MaxResults=50)["UserPools"]]
    out["s3"] = {"count": len(s.client("s3").list_buckets()["Buckets"])}
    ec2 = s.client("ec2")
    out["nat"] = len(ec2.describe_nat_gateways(Filters=[{"Name": "state", "Values": ["available"]}])["NatGateways"])
    addrs = ec2.describe_addresses()["Addresses"]
    out["eips"] = {"total": len(addrs), "unattached": sum(1 for a in addrs if not a.get("AssociationId"))}
    out["ecr"] = len(paginate(s.client("ecr"), "describe_repositories", "repositories"))
    out["secrets"] = len(paginate(s.client("secretsmanager"), "list_secrets", "SecretList"))
    out["dynamodb"] = paginate(s.client("dynamodb"), "list_tables", "TableNames")
    out["ses"] = [i["IdentityName"] for i in s.client("sesv2").list_email_identities()["EmailIdentities"] if "@" not in i["IdentityName"]]
    out["eventbridge"] = [r["Name"] for r in s.client("events").list_rules()["Rules"] if not r["Name"].startswith(("AmazonCloudWatch", "AutoScaling", "EKS"))]
    return out


def collect_cost(s: Any) -> list[list]:
    """Last 30 days by service, from Cost Explorer (a global API, always us-east-1)."""
    ce = s.client("ce", region_name="us-east-1")
    today = date.today()
    resp = ce.get_cost_and_usage(TimePeriod={"Start": str(today - timedelta(days=30)), "End": str(today)}, Granularity="MONTHLY",
                                 Metrics=["UnblendedCost"], GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}])
    totals: dict[str, float] = {}
    for r in resp["ResultsByTime"]:
        for g in r["Groups"]:
            totals[g["Keys"][0]] = totals.get(g["Keys"][0], 0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
    return [[k, round(v, 2)] for k, v in sorted(totals.items(), key=lambda kv: -kv[1]) if v >= 1]


def safe(name: str, fn: Any, *args: Any) -> Any:
    """Run one collector; a missing permission or unused service must not stop the whole inventory."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 - report and keep going
        print(f"warning: {name} skipped ({type(e).__name__}: {str(e)[:120]})")
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--region", required=True)
    ap.add_argument("--profile")
    ap.add_argument("--staging-pattern", help="regex marking non-production names (default: stag|dev|test|qa…)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    s = session(a.profile, a.region)
    cw = s.client("cloudwatch")
    try:
        ident = s.client("sts").get_caller_identity()
    except Exception as e:  # noqa: BLE001 - any credential failure gets the same fix
        raise SystemExit(f"AWS sign-in needed ({type(e).__name__}). Run `aws login` (or `aws sso login --profile …`) and try again.") from e
    ecs = safe("ecs", collect_ecs, s, cw, a.staging_pattern) or []
    out = {"collectedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "region": a.region, "account": ident["Account"][-4:].rjust(12, "•"),
           "ecs": ecs, "ec2": safe("ec2", collect_ec2, s, cw, a.staging_pattern) or [], "rds": safe("rds", collect_rds, s, cw, a.staging_pattern) or [],
           "lb": safe("load balancers", collect_lb, s, cw, ecs) or [], "sqs": safe("sqs", collect_sqs, s, cw) or [],
           "lambda": safe("lambda", collect_lambda, s, cw) or [], "cost": safe("cost explorer", collect_cost, s) or []}
    out.update(safe("other services", collect_misc, s, cw) or {})
    json.dump(out, open(a.out, "w"), separators=(",", ":"))
    print(f"aws inventory: {len(ecs)} ECS services, {len(out['ec2'])} EC2, {len(out['rds'])} databases, {len(out['lb'])} load balancers, "
          f"{len(out['sqs'])} queues, {len(out['lambda'])} Lambdas -> {a.out}")


if __name__ == "__main__":
    main()
