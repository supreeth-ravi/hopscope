# Contributing

Issues and pull requests are welcome.

- **Bugs:** include the `hopscope.py` command you ran, the warning or traceback, and (if you can) the output of `hopscope.py review`. Never paste real account IDs, hostnames or keys.
- **New AWS services or frameworks:** collectors live in `skills/hopscope/scripts/aws_inventory.py`, route detection in `code_scan.py`. Add a test with a synthetic fixture in `tests/`; tests must not need a real AWS account.
- **Checks before a PR:** `ruff check`, `mypy --ignore-missing-imports skills/hopscope/scripts` and `pytest` (see [Development](docs/development.md)).
