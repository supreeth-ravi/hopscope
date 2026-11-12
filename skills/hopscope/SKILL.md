---
name: hopscope
description: Hopscope builds a live ops console from someone's own code and AWS account — a health dashboard (traffic, errors, latency, saturation, an action queue with fixes), an architecture diagram of what is actually deployed, an inventory of every AWS component with usage, a cost and savings view, and a narrated trace that plays any API request hop by hop from DNS and the load balancer rule through the container and code path to databases, queues and background workers. Use this whenever the user wants to see or run ops for their system, map their code to their AWS servers, understand where a request goes ("show me the /login flow on infra"), find what is broken or wasting money in AWS, get an architecture diagram of their deployed stack, set up a dashboard over ECS/EC2/RDS/SQS/Lambda, ask what a code change would break or what an API touches, or want a Mermaid or draw.io diagram of their stack — even if they don't say "Hopscope".
---

# Hopscope

Hopscope turns one AWS account plus the code repositories that run on it into a single page with five views:
**Overview** (health status, the four health signals, action queue), **Architecture** (diagram of what
is deployed, from real routing rules), **AWS components** (every queue, function, cache, bucket…
with usage), **Trace** (ask about any API; it plays the request through the real infrastructure with
a narrator), and **Cost** (spend by service and savings that don't touch production).

Everything is read-only. The collectors only call Describe/List/Get APIs and CloudWatch. Container
environment values are dropped unless they are AWS endpoints or queue URLs, so no secrets are stored.

**In Codex:** `<skill-dir>` below means this skill's folder, the one holding this SKILL.md. Setup,
the AWS calls and `hopscope.py all` need the network, so run them with escalated permissions rather
than in the sandbox. Codex has no `!` prefix, so ask the user to run `aws login` in their own terminal.
There is no Artifact tool, so show the console with `hopscope.py serve` (step 5). In plain words, the
user can ask for "a Hopscope console" or type `$hopscope`.

## Design mode (no AWS)

If there is no AWS sign-in, the user has no AWS account yet, or they ask for a design or architecture
view of what the files describe, skip the AWS steps and run:

```bash
$PY <skill-dir>/scripts/hopscope.py design --repo <path> [--repo <path> …] [--overrides hopscope.json] --workdir <project>/.hopscope
```

It reads docker compose, Terraform, CloudFormation/SAM and Kubernetes files under the repos (plain
manifests, Kustomize overlays as written, Helm charts best effort without helm), links code to the
services they run (exactly from compose `build:` folders and `skaffold.yaml` artifacts, by name matching
otherwise, with doubtful links listed by `hopscope.py review`), and writes the same console. Kubernetes
workloads appear as services in one box per namespace, Services/Ingresses/HTTPRoutes as entry points. Say plainly that it is built
from code and infrastructure files with **no live numbers** (no traffic, health or cost). Environment
values from those files are filtered: only queue names, masked hosts and service names are kept, so no
secrets are stored. Not read yet: Serverless Framework, CDK (unless `cdk synth` output exists),
Pulumi, Helm template logic beyond simple `.Values`, remote Kustomize bases, and Terraform modules,
variables and `for_each`. Once the user signs in to AWS,
offer `hopscope.py all` for the live view.

## Workflow

### 1. Set up and check access

```bash
PY=$(bash <skill-dir>/scripts/setup.sh)      # creates a small venv once (boto3), prints its python
aws sts get-caller-identity                   # must succeed
```

If the AWS call fails, ask the user to sign in themselves: in Claude Code suggest `! aws login` (or
`! aws sso login --profile <name>`), in Codex ask them to run the same command in their terminal. Don't try to handle credentials yourself. The minimum permissions
are in `references/iam-policy.json`; share it if their role lacks read access.

### 2. Ask only what you can't find

You need: the **region**, the **repo folders** for the services that run in that account, and
optionally an AWS **profile**. Look in the current directory and the user's projects first; ask only
for what's missing. If they have prod and staging in one account, the default pattern already treats
names containing stag/dev/test/qa as non-production (`--staging-pattern` overrides it).

### 3. Collect and build

```bash
$PY <skill-dir>/scripts/hopscope.py all --region <region> --repo <path> [--repo <path> …] [--profile <p>] \
    [--overrides hopscope.json] --workdir <project>/.hopscope
```

The inventory also reads EKS clusters, Step Functions, Kinesis, MSK, EventBridge targets and Lambda event
sources. For Kubernetes workloads on EKS (or any cluster), add `--kube-context <ctx>` (or
`--kube-contexts a,b`): it runs read-only `kubectl get` for workloads, services, ingresses, HPAs and events
(never Secrets) and is skipped with a warning if `kubectl` is missing. Only pass contexts the user names.

This runs eight steps (inventory, code map, call flows, linking, hourly metrics, drift, health rules,
snapshot) and writes `<workdir>/site/`. Drift and the snapshot never stop the build; if they fail, it warns. It takes 1–5 minutes on a typical account. Individual steps that lack
permission are skipped with a warning rather than failing the run — mention any warnings to the user.

If a repo already contains a floorplan map (`floorplan/graph.json`), the code map uses its
human-readable names; otherwise the built-in scanner handles Python (FastAPI, Flask), JavaScript and
TypeScript (Express, NestJS), Go, Java and Kotlin (Spring, Micronaut, JAX-RS, Ktor) and C# (ASP.NET Core
controllers and minimal APIs). Call sequences are traced for Python, Java, Kotlin and C# handlers; Go and
JavaScript still get routes, imports and the infrastructure path. HTTP calls in JS/TS frontends are matched to
the backend routes they reach (`clientCalls` in code.json).

### 4. Review the links — this is the step that makes it trustworthy

The linker decides which code folder runs on which ECS service from deployment files, name matching,
container environment and queue names. Run `hopscope.py review --workdir …` and show the user:

- links below 0.6 confidence, with their evidence, and
- running services with no code found.

Settle the obvious ones yourself first: open the candidate folders (Dockerfile, entry point, the routes the service's load balancer rule serves) and decide. Ask the user only about the ones that stay genuinely ambiguous, and list your own decisions so they can overrule them. Mark off-the-shelf software that isn't built from these repos (a model gateway, a tracing UI, a media server) as `null`. Write corrections to `hopscope.json` (format in
`references/hopscope-json.md`) and re-run `hopscope.py all … --overrides hopscope.json`. A wrong link sends traces
to the wrong container, so don't skip this for links the user would act on.

### 5. Show it

- **If the Artifact tool is available**, publish `<workdir>/site/page.html` as the page with
  `files` mapping `code.json`, `aws.json`, `flows.json`, `ts.json`, `model.json`, and `changes.json`
  and `drift.json` when present, to the files in `<workdir>/site/`, and declare `capabilities: {"sample": {}}` so the Analyst panel and "Ask" can
  use Claude. The page is private until the user shares it; say so.
- **Otherwise** (Claude Code in a terminal, or Codex), run
  `$PY <skill-dir>/scripts/hopscope.py serve --workdir …` in the background and give the user
  `http://127.0.0.1:8765/`. The Analyst panel and "Ask" then run on this machine through the Claude
  Code or Codex CLI, on the user's own sign-in: it uses the agent that started it, else whichever is
  installed (`--engine claude|codex` forces one, `--model` picks the model). Each question runs with the
  CLI's tools, shell and plugins off, in an empty folder, without AWS credentials, and isn't saved to
  the CLI's history. If neither CLI is installed the
  console still works, with the built-in analysis. **In Codex**, start the server outside the sandbox
  (escalated permissions): it has to keep running after the command ends, and Ask needs the network.
  If that isn't allowed, give the user the command to run in their own terminal.

Then summarise in a few lines: the overall status, the top 3 actions with their fixes, and the
largest savings. Point them at the Trace tab with one of the generated example questions. If
`hopscope.py drift` reports findings, mention the top two; if an action says errors jumped after a deploy,
point them at the Timeline tab. Drift names are matched loosely: say a finding is likely, not certain, when
it is marked low confidence, and don't treat the "uncertain" list as drift.

### 6. Keep it fresh (offer, don't assume)

The data is a snapshot. Offer to re-run on a schedule (the `schedule` skill, or a cron job running
`hopscope.py all` and republishing to the same artifact URL).

### 7. What the browser really calls (optional)

If the user wants traces backed by real traffic, ask them to record a HAR: in Chrome open DevTools
(View > Developer > Developer Tools), the **Network** tab, tick **Preserve log**, use the app, then right-click
the request list and choose **Save all as HAR with content** (or the download arrow). Then run

```bash
$PY <skill-dir>/scripts/hopscope.py observe --har <file.har> [--har …] [--include-host api.example.com] --workdir <project>/.hopscope
```

Tell the user plainly: **a HAR holds their cookies, tokens and request bodies.** Hopscope reads it on this
machine and keeps only the method, the path pattern (ids become `{id}`), the status and the timing; they
should delete the HAR afterwards and never commit or share it. Don't open or print the HAR yourself. Traces
then say "seen in a browser capture (N requests: …)", and requests no scanned route serves are listed
(`observe` prints them; MCP `observed`). Mention those: they are often a missing repo or a dead endpoint.

## Answering questions once it is built

When `.hopscope/` already holds a build, answer questions about the system from it instead of
re-reading the code or the account.

- **MCP tools (Claude Code).** The plugin registers a `hopscope` MCP server that reads
  `<project>/.hopscope/data` (or `site/`). Use `impact` before changing a file ("what breaks if I change
  `app/identity.py`?": dependent files, the routes whose handler or code path reaches it, the services they
  run on, and the queues and workers downstream), `trace` for "what does POST /orders touch?",
  `find_routes` to get the exact route name first (or pass `trace` a frontend file or `file:line` to start
  from the button that makes the call; its `calledFrom` lists the frontend files that call a route), `who_calls` for callers of a file or function,
  `what_runs` for the compute, queues and database behind a code folder, `resources` for the inventory,
  `health` for one resource's status and the actions that mention it, `changes` for "what changed since
  the last build / before the outage?" (each change, earlier builds, recent ECS deploys and revisions),
  `drift` for "does what's deployed match our Terraform and code?" (each finding with its evidence), and
  `observed` for "which routes did the browser capture hit, and what did it call that no route serves?". In Codex the server is added
  by hand (see `docs/codex.md`). If a tool says there is no data, build first (`all` or `design`).
- **Command line.** The same answers as text:
  `$PY <skill-dir>/scripts/hopscope.py impact <file> --workdir <project>/.hopscope` and
  `$PY <skill-dir>/scripts/hopscope.py query "POST /orders" --workdir <project>/.hopscope`,
  `hopscope.py changes [--since <snapshot>]` and `hopscope.py drift`.
- **Diagrams for a README or a design doc.**
  `hopscope.py export --format mermaid --out arch.mmd` (architecture flowchart),
  `--format mermaid --route "POST /orders" --out orders.mmd` (sequence diagram of one route), or
  `--format drawio --out arch.drawio` (architecture with AWS icons, opens in draw.io). Exports describe
  the real system, so treat them like the console: don't publish one from a real account.

The answers are as good as the build: call sequences are traced for Python, Java, Kotlin and C# handlers, and a route's
compute comes from the reviewed links. A compute marked unconfirmed (`{"name", "confidence", "unconfirmed": true}`
in a tool result, "unconfirmed link, 0.5" in text) is a low-confidence guess: tell the user it is unconfirmed, never
state it as fact, and offer `hopscope.py review`. In design mode there are no live numbers.

## Making it presentable for others

For a public demo or post, never publish a real account's data, even with names replaced: the services,
routes, code paths and numbers still describe the real system. Build the demo from invented data instead.
`references/hopscope-json.md` lists a `company` field that sets the console's brand name.

## When results look wrong

- **Every service in one system / nothing linked** → the repos passed don't match what's deployed
  (wrong folder, or the services are deployed from another repo). Ask which repo builds which image.
- **Traces land on the wrong container** → fix that service in `hopscope.json` → `placement`.
- **Charts are empty** → no public load balancer, or CloudWatch has no data in the last 72 hours
  for the chosen resources (new account, or everything stopped). The rest of the console still works.
- **`AccessDenied` warnings** → the role lacks one of the read permissions; the matching view is
  empty. Compare against `references/iam-policy.json`.
- **Very large accounts** (thousands of Lambdas or queues) are fine; they are grouped into families.

## Files

- `scripts/hopscope.py` — entry point (`all`, `collect`, `design`, `build`, `review`, `serve`, `impact`, `query`, `export`, `changes`, `drift`, `observe`)
- `scripts/iac_k8s.py`, `iac_kustomize.py`, `iac_helm.py`, `k8s_map.py`, `k8s_files.py` — Kubernetes manifests, Kustomize and Helm in design mode
- `scripts/aws_eks.py`, `scripts/aws_streams.py`, `scripts/rules_streams.py` — EKS and kubectl (opt-in), workflows and streams, their rules
- `scripts/linker_streams.py`, `scripts/hopscope_streams.py` — code ↔ streams and their Lambda consumers, in links and traces
- `scripts/observe_har.py`, `scripts/hopscope_observed.py` — browser captures (HAR) matched to routes
- `scripts/history.py` — snapshots of every build (`<workdir>/snapshots/`, last 30) and `changes.json`
- `scripts/drift.py`, `drift_code.py`, `drift_names.py` — drift between code, infrastructure files and AWS (`drift.json`)
- `scripts/hopscope_changes.py` — answers about changes and drift for the CLI and MCP
- `scripts/hopscope_query.py` — answers over a build: routes, traces, callers, change impact, resources, health
- `scripts/hopscope_mcp.py` — MCP server (stdio) exposing those answers as tools
- `scripts/hopscope_export.py` — Mermaid and draw.io exports
- `scripts/local_agent.py` — runs the console's Ask on the Claude Code or Codex CLI when served locally
- `scripts/aws_inventory.py`, `scripts/aws_timeseries.py` — read-only AWS collectors
- `scripts/code_scan.py`, `scripts/call_flow.py` — code map and per-API call sequences
- `scripts/linker.py` — code ↔ infrastructure links with confidence and evidence
- `scripts/rules.py` — health rules, action queue, savings (thresholds overridable)
- `assets/console.html` — the console page (reads the JSON files at load)
- `references/iam-policy.json`, `references/hopscope-json.md`
