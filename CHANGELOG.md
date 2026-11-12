# Changelog

## 0.5.0 — 2026-11-12

- **Snapshots and changes.** Every `all`, `collect` and `design` run copies `aws.json`, `links.json` and `model.json` into `<workdir>/snapshots/<UTC time>/` (with hashes and counts for `code.json` and `flows.json`) and writes `changes.json`: resources added, removed or changed (counts, sizes, images, task-definition revision, load balancer rules, queues, databases), links that moved, and actions that appeared or resolved. The last 30 are kept (`--keep`). `hopscope.py changes [--since <snapshot>]` prints them.
- **Deploy history.** In live mode each ECS service records its task-definition revision and when it was registered, its current deployments, up to five earlier revisions and the last 20 service events other than "steady state" (those are counted), with ARNs reduced to names and account numbers, endpoints, IP addresses and EC2 host names masked. The IAM policy adds `ecs:ListTaskDefinitions` (read-only) for the earlier revisions; without it the current one is still recorded.
- **Drift report.** `drift.json` and `hopscope.py drift` compare the code, the infrastructure files (now also read in live mode, into `iac.json`) and live AWS: resources in AWS but in no file, files never deployed, desired counts, images and task-definition families that differ, queues no code references, queues the code names that don't exist, settings pointing at missing queues or databases, and links that may be wrong (a file row with the service's name builds another folder). AWS and file rows match only on the same name apart from environment words and numbers, a variable prefix, or the same code folder; names that merely share words never produce a finding. Per-tenant queue families are one finding each (referenced when the code contains their static prefix), with counts. Design mode runs the code-to-files checks only.
- **Actions.** The action queue gains up to three drift items with grouped counts, one for a deploy (or new revision) in the three hours before 5xx jumped, and up to three for services whose tasks couldn't start in the last 72 hours (placement, image pull or health-check failures).
- **Console.** A Timeline tab: deploys and builds on one 72-hour axis next to hourly 5xx, with what changed on click; a Drift list on the Timeline and in the Components view. Design mode lists builds and drift with no time axis.
- **MCP.** Two more read-only tools: `changes` and `drift`.
- The Kestrel demo has deploys (shop-api revision 42 shortly before the Sunday checkout outage), three builds and a drifted Terraform file.

## 0.4.1 — 2026-11-12

Fixes from running design mode on a large monorepo:

- **No account numbers in the output.** Design rows keep only an image's repository name (whether it is your own image is still decided from the full reference), the infrastructure scan stars out every standalone 12-digit number (keys included), and link evidence is masked the same way. Longer digit runs, such as epoch suffixes in queue names, are left alone so the names still match the code.
- **Compose build folders.** A `dockerfile:` in a folder below the build context (context at the repo root, `dockerfile: admin/Dockerfile`) now places the service on that folder. Contexts outside the repo are still rejected.
- **Folders with no code stay unplaced.** A service built from a folder that holds no scanned code (SQL, dbt, config) is no longer guessed onto another service by name; links.json records why under `unplacedWhy` and the design checks list it separately.
- **Compose variants.** `docker-compose.<variant>.yml` maps to its own cluster (`<folder>-<variant>`, `compose-<variant>` at the root) and staging/dev/test variants count as staging. Service keys are guaranteed unique across files.
- **`env_file:` is read.** Paths, lists and the `{path, required}` form, next to the compose file and only inside the repo, 500 lines per file. Inline `environment` wins, and every value still goes through the same allowlist, so no credentials are kept; plain host names under `*_HOST` keys are kept, as for inline values.
- **Same-named data stores.** A second `postgres` or `redis` from another compose file shows as `redis (<cluster>)`; its endpoint stays the bare service name and database links prefer the store from the service's own file.
- **hopscope.json folders resolve.** Placement, label and queue entries written for a parent folder (`<repo>/payments`) match the scanned service (`payments`). An entry that matches no scanned folder is not used; a placement goes to review with the reason, and each one is reported as a warning.
- **Linker crash.** An environment value holding only an SQS host (no queue name) no longer stops the linker.
- **Plain-word route queries.** `hopscope.py query checkout`, `find_routes` and the MCP `trace` tool match words against the method, path segments and title; a clearly best route is traced, otherwise the close candidates are listed.

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
