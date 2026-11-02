# How it works

```mermaid
flowchart LR
    aws["AWS account<br/>read-only"] --> inv["Inventory + metrics<br/>aws_inventory · aws_timeseries"]
    code["Your repos"] --> scan["Code map + call flows<br/>code_scan · call_flow"]
    inv --> link["Linker<br/>code ↔ services, queues, databases"]
    scan --> link
    link --> rules["Health rules<br/>status · actions · savings"]
    rules --> page["Console<br/>model.json + console.html"]
```

## Collect

- **Inventory** (`aws_inventory.py`): ECS services with their task definitions, EC2, RDS/Aurora, load balancers with every listener rule and target health, SQS, SNS, Lambda, ElastiCache, OpenSearch, API Gateway, CloudFront, Cognito, S3, ECR, secrets, DynamoDB, SES, EventBridge, and 30 days of cost. Each comes with 3-day CloudWatch usage. Any collector that lacks permission is skipped with a warning.
- **Time series** (`aws_timeseries.py`): hourly series for the busiest public load balancer, the busiest production database, the production container closest to its memory limit and the three busiest queues, plus 14 days of daily cost.

## Read the code

- **Code map** (`code_scan.py`): services (folders with a Dockerfile or package manifest), files, imports, API routes (FastAPI, Flask, Express, NestJS, Go), router prefixes across files, and which external systems each file talks to. An existing floorplan map is used instead when present, for its human-readable names.
- **Call flows** (`call_flow.py`): for every Python handler, the calls in source order, followed into your own functions, recording each call that leaves the process: SQL with table names, AWS SDK calls, cache, queues, HTTP to other services, model calls. Each step records whether it always runs, runs under a condition (and which one), or only on error.

## Link

`linker.py` decides which code folder runs on which ECS service, using:

1. **Deployment files** — Dockerfiles, CI workflows, task definitions, Terraform — that name a service, counted only for the folder they live in or a path named next to the service.
2. **Names** — service and image names against folder names, weighting rare words over common ones and ignoring prefixes shared by most clusters.
3. **Container environment** — queue URLs and database endpoints.
4. **Queue names** found in the code.

Each link has a confidence and its evidence. Below 0.6, or a near-tie between two folders, it goes to `hopscope.py review`. Services running public images are treated as off-the-shelf software. `hopscope.json` overrides anything.

## Rules

`rules.py` turns everything into a status, an action queue and savings: failing load balancer traffic and dead routes, databases at their capacity limit, containers near their memory limit, dead-letter queues with messages, backed-up queues, single-task production services, single EC2 servers carrying a large share of traffic, burstable instances paying for surplus credits, idle staging, disks on stopped servers, idle load balancers, unattached IPs and image-repository sprawl. Thresholds are configurable.
