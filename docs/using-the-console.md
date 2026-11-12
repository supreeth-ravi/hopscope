# Using the console

**Ask bar** (⌘K) — type an API (`POST /account/sign-in`) or a question ("how does an order reach the roaster?") and press **Trace**.

## Overview

- **Status strip** — Healthy, Watch or Degraded, with the two most serious findings.
- **Health tiles** — traffic, errors, latency, saturation, queues and daily spend, each with a 72-hour sparkline.
- **Charts** — requests and 503s per hour on the busiest public load balancer, main database capacity against its limit, the container closest to running out of memory, daily spend. Hover for exact values.
- **Action queue** — every finding with its impact and a concrete fix, critical first.
- **Traffic by service** — requests per target group; routes with no healthy target are hatched red.

## Architecture

A diagram of the deployed system: entry points, global services, the VPC with public, app and data subnets, and managed services. Every service has a status dot. Hover any box to light up its connections; click a service for its details. **Include staging** adds non-production resources.

## AWS components

When the build has a drift report, it comes first. Then load balancers with their routing rules (hostname and path → service), API gateways, CloudFront, Lambda (with idle families), queues (with dead-letter queues and idle families), SNS and EventBridge, databases, caches, search, S3, Cognito, SES, ECR, secrets.

## Trace

The request drawn on the real infrastructure, numbered in order. Solid lines are the request, dashed the response, dotted steps that only run sometimes, and violet the background work after the response.

- **▶ Play request** — a packet travels each hop while a narrator explains it. Choose the **voice** and **speed** in the player bar; your browser's built-in voices are used, so there's no delay.
- **Conditional steps** say when they run, taken from the code: the developer's comment above the `if` when there is one, otherwise the condition in plain words.
- **Sequence** — every step with its file and line.
- **Analyst** — a built-in brief of the path and its risks. **Ask Claude** (or **Ask Codex**) writes a deeper walk-through: in the Claude app it uses your Claude account; served locally with `hopscope.py serve` it uses the Claude Code or Codex CLI on your own sign-in (`--engine` picks one).

## Timeline

Shown when the build has a snapshot history or ECS deploy records.

- **One time axis, the last 72 hours** (live mode): hourly 5xx bars from the busiest public load balancer on top, a lane of Hopscope builds, and a lane per ECS service with its deploys: a marker per task-definition revision (when it was registered) or ECS deployment (when it started). Production services come first.
- **Click a marker** (or focus it and press Enter) to see what changed: for a deploy, the revision it replaced, the deployment state, 5xx in the hour it went out against the peak in the four hours after, and the service's events around it (failed health checks in red); for a build, its diff against the build before. The deploy most followed by errors is selected when the page opens.
- **Since the last build** lists every change from `changes.json`.
- **Drift** lists where the code, the infrastructure files and AWS disagree, each with its evidence; low-confidence findings are marked.
- In design mode there are no deploys or errors, so the builds are listed instead of drawn on an axis.

## Cost

Spend by service for the last 30 days, and savings that don't touch production, each with a rough monthly estimate.
