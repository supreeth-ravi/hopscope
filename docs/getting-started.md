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

## Keeping it fresh

The console is a snapshot. Re-run `hopscope.py all` (with `--overrides hopscope.json`) whenever you want new numbers, or ask the agent to schedule it.

## Removing it

```bash
claude plugin uninstall hopscope@hopscope
claude plugin marketplace remove hopscope
```

Collected data lives in the `.hopscope/` folder you chose; delete it to remove everything.
