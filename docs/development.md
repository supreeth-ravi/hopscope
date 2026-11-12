# Development

```
.claude-plugin/          Claude Code plugin and marketplace manifests, icon
.codex-plugin/           Codex plugin manifest, icon
.agents/plugins/         Codex marketplace manifest
skills/hopscope/
  SKILL.md               how Claude Code or Codex runs Hopscope
  scripts/               collectors, code map (code_scan.py), call flows, linker, rules, CLI (hopscope.py),
                         local Ask on Claude Code or Codex (local_agent.py), answers over a build
                         (hopscope_query.py, hopscope_changes.py), MCP server (hopscope_mcp.py), exports
                         (hopscope_export.py), snapshots and changes (history.py), drift (drift.py, drift_code.py, drift_names.py)
  scripts/langs/         one module per language the code scanner reads (the plugin interface below)
  assets/console.html    the console page
  references/            IAM policy, hopscope.json reference
docs/                    documentation, images, and the live demos (docs/demo, docs/demo-k8s, docs/demo-serverless)
tests/                   pytest suite with synthetic repos and inventories
```

```bash
PY=$(bash skills/hopscope/scripts/setup.sh)
uv pip install --python $PY pytest pytest-cov mypy ruff     # or: $PY -m pip install …
$PY -m ruff check skills/hopscope/scripts tests --select E,F,B --ignore E501
$PY -m mypy --ignore-missing-imports skills/hopscope/scripts
$PY -m pytest tests --cov=skills/hopscope/scripts
```

Tests never touch a real AWS account. The demo in `docs/demo` is a fictional company, Kestrel Coffee: its code, AWS setup, traffic and cost are all invented. `python docs/demo/make_demo.py` rebuilds it by running Hopscope's own pipeline over a small invented codebase (with a Terraform file) and AWS inventory, three times at invented build times so the demo has snapshots, deploys and drift; never build the demo from a real account. `python docs/demo-k8s/make_k8s.py` builds Kestrel Coffee on Kubernetes (invented `kubectl get` output through the live Kubernetes mapper, two builds) and `python docs/demo-serverless/make_serverless.py` a serverless Kestrel Coffee in two regions (two invented regional inventories merged by `aws_merge`). Each demo folder holds its own `index.html`: the console template between the same head and closing tags `hopscope.py build` writes, so after changing `assets/console.html` re-embed it in every `docs/demo*/index.html`.

## Adding a language

`code_scan.py` is a driver: everything language-specific lives in `skills/hopscope/scripts/langs/`, one module per
language (`python.py`, `js.py`, `go.py`, …). The full contract is in the docstring of `langs/__init__.py`; in short a
module defines:

| Name | What it is |
|---|---|
| `NAME`, `EXTENSIONS` | the language id and the file suffixes it reads |
| `SERVICE_MARKERS` | files that mark a deployable folder (`go.mod`, `pom.xml`, `*.csproj`, `Gemfile`, `composer.json`, `Cargo.toml`; globs allowed) |
| `routes(path, text, ctx)` | the API routes in one file: `{"m", "p", "t", "d", "l"}` (method, path, title, description, line); a route may carry `"f"` (a file index) when its handler is in another file, and `"l"` is then the handler's line |
| `imports(path, text, ctx)` | indices of the scanned files it imports (`ctx.index` maps a resolved path to its index) |
| `externals(text, ctx)` | the external systems it talks to, as console keys (`postgres`, `sqs`, `kafka`, …) |
| optional `prepare(files)` | one pass over all of the language's files first (e.g. a package index); its result is `ctx.state` |
| optional `mounts`, `TEST_FILE`, `BUILD_DIRS` | router prefixes, test-file names to skip, build-output folders to skip |
| optional `client_calls(path, text)` | HTTP calls a frontend file makes, as `(line, method, path)`; the driver matches them to routes (`langs/clientmatch.py`) |
| optional `flows(data, base, route_index)` | the handler's call sequence in `flows.json`'s step shape; used by `call_flow.py` |

Steps:

1. Write the module with the standard library only (regular expressions and brace matching are enough; see
   `langs/java.py` and `langs/common.py` for comment blanking, brace matching and string literals).
2. Add it to `LANGUAGES` in `langs/__init__.py`. `tests/test_langs.py` checks the contract.
3. Add tests with a small invented fixture project (`tests/test_lang_<name>.py`; the Kestrel Coffee projects in
   `tests/kestrel_fixtures.py` are examples): routes with their prefixes,
   imports, externals and the service folder. Keep `tests/test_code_scan_golden.py` passing: the existing languages'
   output must not change.
4. If the console should draw a new external kind, add it to `EXTERNAL` (and `EXT_NODE` when it maps onto an AWS
   component) in `assets/console.html`.
5. Mention the language in the README, `docs/how-it-works.md` and `skills/hopscope/SKILL.md`.

Try a local change in Claude Code without publishing:

```bash
claude plugin marketplace add ./
claude plugin install hopscope@hopscope
```
