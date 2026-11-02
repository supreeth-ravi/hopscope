# hopscope.json — manual corrections and settings

Optional. Pass with `--overrides hopscope.json`. Every field is optional; anything set here wins over what
the collectors infer.

```json
{
  "company": "Kestrel Coffee",
  "placement": {
    "prod-cluster/payments-api": "services/payments",
    "prod-cluster/email-worker": "services/email",
    "prod-cluster/model-gateway": null
  },
  "labels": {
    "services/payments": "Payments",
    "services/email": "Email sender"
  },
  "queues": {
    "services/payments": ["payment-events.fifo"]
  },
  "queueWorkers": {
    "payment-events.fifo": ["payments-worker", "Postgres: write ledger entry"]
  },
  "thresholds": {
    "err_pct_critical": 5,
    "mem_critical": 85,
    "mem_warn": 75,
    "cpu_warn": 85,
    "db_pinned_hours": 12,
    "db_cpu_critical": 90,
    "single_ec2_share": 0.25,
    "queue_age_warn_s": 3600,
    "ecr_repos_warn": 200
  },
  "examples": ["Show me POST /account/sign-in", "Show me POST /orders"]
}
```

| Field | What it does |
|---|---|
| `company` | Brand name in the header and diagram title. |
| `placement` | `"<cluster>/<ecs service>": "<code folder>"`, or `null` for off-the-shelf software not built from these repos. The code folder is relative to the repo (or to the common parent of several repos), as shown by `hopscope.py review`. |
| `labels` | Friendly names for code folders, shown as system names. |
| `queues` | Queues a code folder sends to or reads from, when the names aren't in the code. |
| `queueWorkers` | `"<queue>": ["<ecs service that consumes it>", "<what the job does>"]`. Drives the "after response" part of traces. |
| `thresholds` | Health rule limits (see `scripts/rules.py`). |
| `examples` | The example questions offered on the Trace tab. |
