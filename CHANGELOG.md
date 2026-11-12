# Changelog

## 0.4.0 — 2026-11-12

- **MCP server.** `hopscope_mcp.py` gives agents seven read-only tools over a build: `find_routes`, `trace`, `who_calls`, `what_runs`, `impact`, `resources` and `health`. Standard-library Python over stdio (MCP 2025-06-18, older 2025-03-26 and 2024-11-05 clients accepted). The Claude Code plugin registers it for `<project>/.hopscope`; Codex users add it with `codex mcp add` (see docs/codex.md).
- **Change impact.** `hopscope.py impact <file>`: the files that depend on it, the routes whose handler or code path reaches it (nearest first), the services they run on, and the queues and workers downstream.
- **Route answers on the command line.** `hopscope.py query "POST /orders"` prints the load balancer rule, compute, code path with conditions, queues, workers and database.
- **Exports.** `hopscope.py export --format mermaid|drawio [--route …] --out FILE`: a Mermaid architecture flowchart, a Mermaid sequence diagram of one route, or a draw.io file of the architecture with AWS icons. Labels are escaped.

## 0.3.0 — 2026-11-12

- **Design mode.** `hopscope.py design --repo <path>` builds the console from your code and your docker compose, Terraform and CloudFormation/SAM files, with no AWS account or credentials. The Architecture, Components and Trace views work; there are no live numbers and the Cost tab is hidden.
- **Exact links.** A compose `build:` folder places a service on its code with full confidence. In design mode a queue links to code only through a real reference (a quoted name, a queue URL, or a `...QUEUE` environment variable), not a substring.
- **Environment-value filter.** Values read from infrastructure files are allowlisted: queue names, masked hosts and service names are kept; credential-like values are dropped. Engine and size labels are sanitised, and the console escapes everything read from files.
- **Design checks.** Routes with nothing serving them, queues with no dead-letter queue, production services planned with one task, and services with no code found.
- **Console design view.** "Design" status, planned task counts instead of CPU and memory, skipped files noted in the footer, no DNS box, no Cost tab.
- New dependency: PyYAML.

## 0.2.0 — 2026-11-02

- **Codex support.** Install from the Codex plugin marketplace (`codex plugin marketplace add supreeth-ravi/hopscope`) and ask for a Hopscope console or type `$hopscope`.
- **Analyst and Ask in the local console.** `hopscope.py serve` answers the console's questions with the Claude Code or Codex CLI on your own sign-in: tools, shell and plugins off, an empty temporary folder, no AWS credentials, nothing saved to history. `--engine claude|codex` and `--model` choose. The endpoint answers only same-origin requests carrying a per-run token.

## 0.1.0 — 2026-11-02

First release.

- **Overview**: health status, traffic / errors / latency / saturation for 72 hours, and an action queue with concrete fixes.
- **Architecture**: system-design diagram of what is deployed, wired from load balancer rules and code dependencies.
- **AWS components**: inventory of load balancers, Lambda, SQS, SNS, ElastiCache, OpenSearch, S3, Cognito, SES, ECR and more, with usage.
- **Trace**: any API played hop by hop from DNS through the load balancer rule, container and code path to data stores, queues and workers; conditional steps explain when they run; voice narration.
- **Cost**: 30-day spend by service and savings that don't touch production.
- Code map from the built-in scanner (Python, JavaScript/TypeScript, Go) or an existing floorplan map.
- Linker with confidence, evidence and a review list; `hopscope.json` for corrections.
- Read-only collectors; secrets dropped, account numbers masked; local server answers only on localhost.
