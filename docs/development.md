# Development

```
.claude-plugin/          Claude Code plugin and marketplace manifests, icon
.codex-plugin/           Codex plugin manifest, icon
.agents/plugins/         Codex marketplace manifest
skills/hopscope/
  SKILL.md               how Claude Code or Codex runs Hopscope
  scripts/               collectors, code map, call flows, linker, rules, CLI (hopscope.py),
                         local Ask on Claude Code or Codex (local_agent.py)
  assets/console.html    the console page
  references/            IAM policy, hopscope.json reference
docs/                    documentation, images, and the live demo (docs/demo)
tests/                   pytest suite with synthetic repos and inventories
```

```bash
PY=$(bash skills/hopscope/scripts/setup.sh)
uv pip install --python $PY pytest pytest-cov mypy ruff     # or: $PY -m pip install …
$PY -m ruff check skills/hopscope/scripts tests --select E,F,B --ignore E501
$PY -m mypy --ignore-missing-imports skills/hopscope/scripts
$PY -m pytest tests --cov=skills/hopscope/scripts
```

Tests never touch a real AWS account. The demo in `docs/demo` uses a fictional company, Acme: every name, host and path is replaced and traffic and cost are scaled.

Try a local change in Claude Code without publishing:

```bash
claude plugin marketplace add ./
claude plugin install hopscope@hopscope
```
