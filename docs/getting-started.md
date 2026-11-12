# Getting started

## What you need

- Claude Code or Codex, signed in.
- Python 3.10 or newer (Hopscope creates its own small virtual environment).
- The AWS CLI, signed in to the account you want to see: `aws login`, or `aws sso login --profile <name>`. The role needs the read-only permissions in [`iam-policy.json`](../skills/hopscope/references/iam-policy.json).

## Install

```bash
claude plugin marketplace add supreeth-ravi/hopscope
claude plugin install hopscope@hopscope
```

In Codex: `codex plugin marketplace add supreeth-ravi/hopscope`, then `codex plugin add hopscope@hopscope` (or add the marketplace from the app's **Plugins** page). See [Hopscope in Codex](codex.md).

## First run

Open Claude Code or Codex in the folder that holds your service repos and ask:

> Build a Hopscope for our AWS account in eu-west-1 and these repos

It will:

1. Check your AWS sign-in (if it has expired, it asks you to run `aws login`).
2. Find your repos and ask only for what it can't work out.
3. Collect: inventory, code map, call flows, links, hourly metrics, drift, health rules, and a snapshot of the build. Usually 1–5 minutes.
4. **Review the links.** It settles the obvious code ↔ service links itself and asks you about the genuinely ambiguous ones. Your answers go in `hopscope.json`.
5. Publish the console (privately) in the Claude app, or serve it at `http://127.0.0.1:8765/`, where the Analyst uses the Claude Code or Codex CLI on your sign-in.

## Without an agent

```bash
PY=$(bash skills/hopscope/scripts/setup.sh)
$PY skills/hopscope/scripts/hopscope.py all --region eu-west-1 --repo ~/src/api --repo ~/src/web
$PY skills/hopscope/scripts/hopscope.py review
$PY skills/hopscope/scripts/hopscope.py serve
```

### Several regions or accounts

```bash
$PY skills/hopscope/scripts/hopscope.py all --region eu-west-1,us-east-1 --repo ~/src/shop      # named regions
$PY skills/hopscope/scripts/hopscope.py all --all-regions --repo ~/src/shop                     # every region enabled for the account
$PY skills/hopscope/scripts/hopscope.py all --region eu-west-1 --profile prod,shared --repo …   # one account per profile
```

Regions are read four at a time; a region with nothing in it is left out, and one that fails is skipped with a warning. The console gets a region (and account) filter, every card says where it runs, and the architecture groups clusters by region. Accounts are shown by their alias, or as `acct-••••1234` when the role may not read it; the account number itself is never written anywhere. When two regions or accounts use the same name (a `kestrel` cluster in both regions), both get a prefix such as `eu-west-1:kestrel`; use that name in `hopscope.json` placement.

**Every account in an organization (opt-in).** Sign in to the management account (or a delegated administrator) and run:

```bash
$PY skills/hopscope/scripts/hopscope.py all --all-regions --org-role HopscopeReadOnly [--accounts 111122223333,444455556666] --repo …
```

Hopscope lists the organization's active accounts (`organizations:ListAccounts`) and assumes `arn:aws:iam::<id>:role/HopscopeReadOnly` in each with `sts:AssumeRole` (session name `hopscope`, one hour). Set that role up once per account:

1. Create a role named, for example, `HopscopeReadOnly` in each member account and attach [`iam-policy.json`](../skills/hopscope/references/iam-policy.json) to it as its only permissions policy (read-only: Describe/List/Get, CloudWatch metrics, Cost Explorer, plus `iam:ListAccountAliases` and `ec2:DescribeRegions`). StackSets can create it in every account.
2. Let the account you run Hopscope from assume it (the role's trust policy):

   ```json
   {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::<management-account-id>:root"}, "Action": "sts:AssumeRole"}]}
   ```

3. Give the identity you run Hopscope as `organizations:ListAccounts` and `sts:AssumeRole` on `arn:aws:iam::*:role/HopscopeReadOnly`.

Each AssumeRole call also passes an inline session policy listing only the read actions of `iam-policy.json`, so the session can't do more than read even if the role allows more. An account without the role, or one that refuses, is skipped with a warning naming only its masked label. `--accounts` limits the run to those ids; `--org-role` takes at most one `--profile` (the account that lists the organization). If no region or account could be read at all, the run stops with the reasons instead of building an empty console. With `--kube-context`, each context is its own scope in the filter (`kube:<context>`).

### Kubernetes on EKS

The inventory lists your EKS clusters and node groups. To also read the workloads, name the kubeconfig context (as `kubectl config get-contexts` shows it):

```bash
$PY skills/hopscope/scripts/hopscope.py all --region eu-west-1 --repo ~/src/shop --kube-context prod-eks
# several clusters: --kube-contexts prod-eks,staging-eks
```

Hopscope runs `kubectl --context <ctx> get deploy,sts,ds,svc,ing,hpa,cronjob -A -o json` and `kubectl get events -A -o json`, read-only, with a timeout; it never asks for Secrets, and skips this step with a warning when `kubectl` is not installed. Your kubeconfig user needs `get`/`list` on those kinds and on events.

### What the browser really calls (HAR)

1. In Chrome, open DevTools (**View → Developer → Developer Tools**) and the **Network** tab, tick **Preserve log**, and use the app.
2. Right-click the request list and choose **Save all as HAR with content** (or click the download arrow).
3. Run:

```bash
$PY skills/hopscope/scripts/hopscope.py observe --har ~/Downloads/shop.kestrel.example.har [--har another.har] [--include-host api.example.com]
```

HAR files up to 100 MB are read; an API on another domain than the page needs `--include-host`. Routes the capture hit are marked in traces and in the MCP `trace` and `observed` tools, and requests no scanned route serves are listed. **A HAR contains your cookies, tokens and request bodies.** Hopscope reads it on your machine and keeps only the method, the path with ids replaced by `{id}`, the status and the timing; delete the HAR afterwards and never commit or share it.

## Without AWS (design mode)

No AWS account, or not deployed yet? Hopscope can build the console from your code and your infrastructure files alone:

```bash
PY=$(bash skills/hopscope/scripts/setup.sh)
$PY skills/hopscope/scripts/hopscope.py design --repo ~/src/api --repo ~/src/web
$PY skills/hopscope/scripts/hopscope.py serve
```

It reads docker compose files, Terraform (`.tf`), CloudFormation or SAM templates (YAML or JSON), and Kubernetes: plain manifests, Kustomize overlays and Helm charts (read without helm; a `rendered/` or `manifests/` folder of pre-rendered output is preferred), with `skaffold.yaml` tying each image to the folder that builds it. You get the Architecture, Components and Trace views and a short list of design checks (routes with no service, queues with no dead-letter queue, production services planned with one task, services with no code found). There is no traffic, health or cost, and the Cost tab is hidden. No credentials are read or needed.

Design mode is a first step: when you have an AWS account to read, `hopscope.py all` gives the live view. See [how it works](how-it-works.md#design-mode) for what the readers do and do not understand.

## Asking questions and exporting diagrams

After a build, Claude Code sessions in the project folder can use the plugin's `hopscope` MCP tools: ask "what breaks if I change app/identity.py?" or "what does POST /orders touch?" and the agent answers from `.hopscope/` without re-reading the code. (In Codex, add the server once: see [Hopscope in Codex](codex.md#mcp-server).) The server looks for `.hopscope/` in the project folder; if you built with another `--workdir`, ask the agent to use the command line below instead.

From the command line:

```bash
$PY skills/hopscope/scripts/hopscope.py impact app/identity.py
$PY skills/hopscope/scripts/hopscope.py query "POST /orders"
$PY skills/hopscope/scripts/hopscope.py export --format mermaid --out architecture.mmd
$PY skills/hopscope/scripts/hopscope.py export --format mermaid --route "POST /orders" --out orders.mmd
$PY skills/hopscope/scripts/hopscope.py export --format drawio --out architecture.drawio
$PY skills/hopscope/scripts/hopscope.py changes                      # since the last build; --since <snapshot> for an older one
$PY skills/hopscope/scripts/hopscope.py drift                        # where code, infrastructure files and AWS disagree
```

The MCP server gives the same two answers as the `changes` and `drift` tools.

## Keeping it fresh

The console is a snapshot. Re-run `hopscope.py all` (with `--overrides hopscope.json`) whenever you want new numbers, or ask the agent to schedule it. Each run keeps a copy of the inventory, links and model in `.hopscope/snapshots/` (the last 30; `--keep N` changes that) and records what changed since the run before, so regular runs build up a history that the Timeline tab and `hopscope.py changes` show.

## Removing it

```bash
claude plugin uninstall hopscope@hopscope
claude plugin marketplace remove hopscope
```

Collected data lives in the `.hopscope/` folder you chose; delete it to remove everything.
