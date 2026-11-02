# Security

Hopscope reads your AWS account and your code, so it is built to keep both safe:

- AWS access is read-only (see [`iam-policy.json`](skills/hopscope/references/iam-policy.json)).
- Container environment values are dropped unless they are a queue URL or the bare hostname of a database, cache or search endpoint; connection strings lose user, password, path and query.
- Account numbers are masked in stored data.
- `hopscope.py serve` binds to 127.0.0.1 and refuses requests whose Host header isn't localhost.
- Its Ask endpoint also requires a same-origin request carrying a token generated at start-up, so other websites open in your browser can't use it. It is meant for a single-user machine: other accounts on the same computer can reach localhost too.
- Ask runs the Claude Code CLI with its tools, settings, hooks, skills and MCP servers off, or the Codex CLI in its read-only sandbox with its shell, plugins, apps, browser and sub-agents off and your Codex config ignored. Either way it starts in an empty temporary folder, without AWS credentials or other secrets from the environment, keeps nothing in the CLI's history, and returns no CLI output to the browser beyond a short error code.
- Text from scanned code is escaped before it is shown in the console.

To report a vulnerability, open a [private security advisory](https://github.com/supreeth-ravi/hopscope/security/advisories/new) rather than a public issue.
