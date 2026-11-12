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
3. Collect: inventory, code map, call flows, links, hourly metrics, health rules. Usually 1–5 minutes.
4. **Review the links.** It settles the obvious code ↔ service links itself and asks you about the genuinely ambiguous ones. Your answers go in `hopscope.json`.
5. Publish the console (privately) in the Claude app, or serve it at `http://127.0.0.1:8765/`, where the Analyst uses the Claude Code or Codex CLI on your sign-in.

## Without an agent

```bash
PY=$(bash skills/hopscope/scripts/setup.sh)
$PY skills/hopscope/scripts/hopscope.py all --region eu-west-1 --repo ~/src/api --repo ~/src/web
$PY skills/hopscope/scripts/hopscope.py review
$PY skills/hopscope/scripts/hopscope.py serve
```

## Without AWS (design mode)

No AWS account, or not deployed yet? Hopscope can build the console from your code and your infrastructure files alone:

```bash
PY=$(bash skills/hopscope/scripts/setup.sh)
$PY skills/hopscope/scripts/hopscope.py design --repo ~/src/api --repo ~/src/web
$PY skills/hopscope/scripts/hopscope.py serve
```

It reads docker compose files, Terraform (`.tf`) and CloudFormation or SAM templates (YAML or JSON) found under the repos. You get the Architecture, Components and Trace views and a short list of design checks (routes with no service, queues with no dead-letter queue, production services planned with one task, services with no code found). There is no traffic, health or cost, and the Cost tab is hidden. No credentials are read or needed.

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
```

## Keeping it fresh

The console is a snapshot. Re-run `hopscope.py all` (with `--overrides hopscope.json`) whenever you want new numbers, or ask the agent to schedule it.

## Removing it

```bash
claude plugin uninstall hopscope@hopscope
claude plugin marketplace remove hopscope
```

Collected data lives in the `.hopscope/` folder you chose; delete it to remove everything.
