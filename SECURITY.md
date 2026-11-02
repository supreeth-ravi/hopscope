# Security

Hopscope reads your AWS account and your code, so it is built to keep both safe:

- AWS access is read-only (see [`iam-policy.json`](skills/hopscope/references/iam-policy.json)).
- Container environment values are dropped unless they are a queue URL or the bare hostname of a database, cache or search endpoint; connection strings lose user, password, path and query.
- Account numbers are masked in stored data.
- `hopscope.py serve` binds to 127.0.0.1 and refuses requests whose Host header isn't localhost.
- Text from scanned code is escaped before it is shown in the console.

To report a vulnerability, open a [private security advisory](https://github.com/supreeth-ravi/hopscope/security/advisories/new) rather than a public issue.
