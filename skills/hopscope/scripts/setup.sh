#!/usr/bin/env bash
# Create the hopscope Python environment once (boto3 with support for `aws login` credentials).
set -euo pipefail
VENV="${HOPSCOPE_VENV:-$HOME/.cache/hopscope/venv}"
if [ ! -x "$VENV/bin/python" ]; then
  if command -v uv >/dev/null 2>&1; then uv venv -q "$VENV"; uv pip install -q --python "$VENV/bin/python" -r "$(dirname "$0")/requirements.txt"
  else python3 -m venv "$VENV" && "$VENV/bin/pip" install -q -r "$(dirname "$0")/requirements.txt"; fi
fi
echo "$VENV/bin/python"
