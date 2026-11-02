# Changelog

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
