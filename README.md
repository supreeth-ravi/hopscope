# Hopscope

**See where every request actually goes — from the load balancer rule, through your code, to the databases and queues it touches — on the AWS you really run.**

Hopscope reads your AWS account (read-only) and your code repositories, links the two together, and gives you one page to run ops from. Ask *"show me POST /account/sign-in"* and it plays that request hop by hop on a diagram of your real infrastructure, with a narrator explaining each step and why it happens.

It ships as a [Claude Code](https://claude.com/claude-code) plugin: you ask Claude for it in plain words, Claude runs the collectors, checks the doubtful links with you, and publishes the page.

---

## What you get

| View | What it shows |
|---|---|
| **Overview** | Overall status, the four health signals (traffic, errors, latency, saturation) for the last 72 hours, and an **action queue**: each problem with its impact and a concrete fix. |
| **Architecture** | A system-design diagram of what is deployed — public and internal load balancers, ECS clusters and their services, EC2, databases, caches and managed services — wired from your real routing rules and your code's dependencies. Hover to trace connections, click for details. |
| **AWS components** | Every load balancer rule, Lambda, queue, topic, cache, search domain and bucket count, with 3-day usage. Families of near-identical resources are grouped; idle ones and failing dead-letter queues stand out. |
| **Trace** | Pick or ask for any API. Hopscope draws the request through DNS → load balancer rule → container → the handler's code path → Postgres, Cognito, SQS, SES, S3, other services → the background worker that picks up its message. Conditional steps say *when* they run (*"if a manager email is configured…"*), taken from the code. Press play to watch it hop, with voice. |
| **Cost** | Last 30 days of spend by service, and a list of savings that don't touch production (idle load balancers, disks on stopped servers, staging that never sleeps, unattached IPs…). |

An analyst panel gives a plain-language brief for every trace; with Claude available it writes a deeper walk-through of the risks on that path.

## Why

Metrics tools show you graphs but not your code. Diagram tools draw your AWS but don't know your code. Service catalogues are filled in by hand and drift. Hopscope joins the two automatically — **which code runs on which container, which queue it writes to, which worker reads it** — and keeps a human in the loop only where the evidence is weak.

## Quick start

### With Claude Code (recommended)

```bash
claude plugin marketplace add supreeth-ravi/hopscope
claude plugin install hopscope@hopscope
```

Or from inside a Claude Code session: `/plugin marketplace add supreeth-ravi/hopscope`, then `/plugin install hopscope@hopscope`.

Then just ask:

> *Build a Hopscope for our AWS account in eu-west-1 and the repos in ~/src*

Claude checks your AWS sign-in, finds the repos, runs the collection (1–5 minutes), resolves the obvious links itself, asks you about the genuinely ambiguous ones, and publishes the page.

### From the command line

```bash
PY=$(bash skills/hopscope/scripts/setup.sh)          # one-time: small venv with boto3
aws sts get-caller-identity                            # make sure you're signed in

$PY skills/hopscope/scripts/hopscope.py all --region eu-west-1 --repo ~/src/backend --repo ~/src/web
$PY skills/hopscope/scripts/hopscope.py review         # confirm low-confidence code ↔ service links
$PY skills/hopscope/scripts/hopscope.py serve          # open http://127.0.0.1:8765/
```

Add corrections to a `hopscope.json` and re-run with `--overrides hopscope.json`.

## How it works

```
 AWS (read-only)                     Your repositories
 ───────────────                     ─────────────────
 aws_inventory.py   ECS, EC2, RDS,   code_scan.py    services, files, API routes,
                    load balancers,                  imports, external systems
                    queues, Lambda…  call_flow.py    each handler's call sequence,
 aws_timeseries.py  hourly metrics,                  with the condition for each step
                    daily cost
                 \                  /
                  linker.py   which code runs where, which queues and databases it uses,
                              which worker consumes each queue — with confidence + evidence
                       |
                  rules.py    health rules → status, action queue, savings
                       |
                  model.json + data files → console.html
```

**Linking code to infrastructure** uses several kinds of evidence, scored together:

1. Deployment files (Dockerfiles, CI workflows, task definitions, Terraform) that name a service — counted only for the folder they live in.
2. Service and image names matched against folder names, weighting distinctive words over common ones and ignoring company-wide prefixes.
3. Queue URLs and database endpoints in container environment variables.
4. Queue names that appear in the code.

Every link gets a confidence and the evidence behind it. Anything below 0.6, or a near-tie between two folders, goes to `hopscope.py review`. Services running public images (a model gateway, a tracing UI) are recognised as off-the-shelf and left unlinked.

**The code map** comes from the built-in scanner (Python: FastAPI, Flask; JavaScript/TypeScript: Express, NestJS; Go), which also resolves router prefixes across files. If a repository already has a floorplan map (`floorplan/graph.json`), Hopscope uses its human-readable module and file names instead.

## Privacy and permissions

- **Read-only.** Only Describe/List/Get calls, CloudWatch `GetMetricData` and Cost Explorer. The exact policy is in [`skills/hopscope/references/iam-policy.json`](skills/hopscope/references/iam-policy.json).
- **No secrets stored.** Container environment values are dropped unless they are AWS endpoints or queue URLs, and account numbers in those are masked.
- **Everything stays local** until you choose to publish. Published pages are private until you share them.
- Collectors that lack a permission are skipped with a warning; the matching view is simply empty.

## Configuration — `hopscope.json`

All fields are optional. Full reference: [`skills/hopscope/references/hopscope-json.md`](skills/hopscope/references/hopscope-json.md).

```json
{
  "company": "Kestrel Coffee",
  "placement": {
    "prod-cluster/payments-api": "services/payments",
    "prod-cluster/model-gateway": null
  },
  "labels": { "services/payments": "Payments" },
  "queueWorkers": { "payment-events.fifo": ["payments-worker", "Postgres: write ledger entry"] },
  "thresholds": { "err_pct_critical": 5, "mem_critical": 85 }
}
```

`null` in `placement` marks a service as not built from these repos.

## What it covers

| Area | Supported |
|---|---|
| Compute | ECS (Fargate and EC2 launch types), EC2 |
| Entry points | Application and Network Load Balancers, API Gateway (REST, HTTP, WebSocket), CloudFront |
| Data | RDS and Aurora (serverless v2 and provisioned), ElastiCache, OpenSearch, DynamoDB, S3 |
| Messaging | SQS (including dead-letter queues), SNS, EventBridge |
| Identity and email | Cognito, SES |
| Code | Python, JavaScript/TypeScript and Go routes; call sequences with conditions for Python |

**Not yet:** EKS/Kubernetes, several regions in one view, GCP and Azure, call sequences for languages other than Python, and scheduled refresh built in (Claude can set one up for you).

## Project layout

```
.claude-plugin/          plugin and marketplace manifests
skills/hopscope/
  SKILL.md               how Claude runs Hopscope for a user
  scripts/               collectors, code map, linker, rules, CLI (hopscope.py)
  assets/console.html    the console page
  references/            IAM policy, hopscope.json reference
tests/                   pytest suite (synthetic repos and inventories, no AWS needed)
```

## Development

```bash
PY=$(bash skills/hopscope/scripts/setup.sh)
uv pip install --python $PY pytest pytest-cov
$PY -m pytest tests --cov=skills/hopscope/scripts
```

The AWS collectors and the CLI are not yet covered by tests; contributions with mocked AWS fixtures are welcome.

---

Built by [Supreeth Ravi](https://supreethravi.com) · MIT licence
