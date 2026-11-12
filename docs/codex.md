# Hopscope in Codex

Hopscope works the same in Codex as in Claude Code: Codex reads your AWS account (read-only) and your code, links the two, asks you about the links it isn't sure of, and opens the console.

## Install

In the Codex app, open **Plugins**, choose **Add a marketplace**, enter `supreeth-ravi/hopscope`, then install **Hopscope**.

From the command line:

```bash
codex plugin marketplace add supreeth-ravi/hopscope
codex plugin add hopscope@hopscope
```

You also need Python 3.10+ and the AWS CLI signed in to your account (`aws login`, or `aws sso login --profile <name>`), run in your own terminal.

## Use it

Start a new chat in the folder that holds your service repos and ask:

> Build a Hopscope console for our AWS account in eu-west-1 and these repos

or type `$hopscope`. Codex will ask for permission to run outside its sandbox for the setup, the AWS calls and the local server, because all three need the network, and the server has to keep running after the command ends. If you'd rather not grant that, Codex gives you the commands to run yourself.

## The console's Analyst

Codex has no published pages, so the console runs on your machine at `http://127.0.0.1:8765/`. Its **Analyst** panel and **Ask Codex** button send each question to the Codex CLI on your own sign-in, with no API key:

- The question already contains the trace and metrics, so Codex gets nothing else to work with. `codex exec` runs in its read-only sandbox with its shell, plugins, apps, browser and sub-agents switched off, ignores your Codex config, and starts in an empty temporary folder. AWS credentials and other secrets in the server's environment are not passed on.
- Runs are ephemeral, so nothing is added to your Codex history.
- The endpoint only answers the console page itself: requests must come from localhost, from the same origin, and carry a token that changes every time the server starts. Other people with an account on the same computer could still reach it, so use it on your own machine.

`hopscope.py serve` picks the agent that started it, and falls back to whichever of Codex or Claude Code is installed. To choose one yourself:

```bash
$PY skills/hopscope/scripts/hopscope.py serve --engine codex [--model <name>]
```

If neither CLI is installed, the console still works with its built-in analysis.

## Differences from Claude Code

| | Claude Code | Codex |
|---|---|---|
| Start it | Ask, or `/hopscope:hopscope` | Ask, or `$hopscope` |
| AWS sign-in | `! aws login` inside the session | `aws login` in your terminal |
| Where the console opens | A private page in the Claude app, or `http://127.0.0.1:8765/` | `http://127.0.0.1:8765/` |
| Analyst and Ask | Your Claude account | Your Codex sign-in |

## MCP server

The Claude Code plugin registers Hopscope's MCP server itself. In Codex, add it once with the path to the plugin's script and your project's `.hopscope` folder (absolute paths, so it works from any folder):

```bash
codex mcp add hopscope -- python3 /path/to/hopscope/skills/hopscope/scripts/hopscope_mcp.py --workdir /path/to/your/project/.hopscope
```

The tools (`impact`, `trace`, `find_routes`, `who_calls`, `what_runs`, `resources`, `health`, `changes`, `drift`) read the build in that folder; run a build first. The server only reads those files: no network, no AWS, no processes, no writes.

The plugin doesn't bundle the server for Codex: Codex does not fill in the plugin's install path (`${CLAUDE_PLUGIN_ROOT}`) in MCP server arguments the way Claude Code does, so the registration Claude Code uses would not start there.

