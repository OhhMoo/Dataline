#!/usr/bin/env bash
# Cloud Agent bootstrap for the Alkera-DE benchmark.
#
# One interpreter runs every component. gateway/.venv (created here by uv from
# gateway/uv.lock) holds duckdb + dbt-duckdb + pyyaml, which is exactly what the
# world builder, the task harness, and the contracts library import, so the same
# gateway/.venv/bin/python drives all of world/, tasks/ and gateway/.
#
# Idempotent: re-running only re-resolves the (locked) dependency set.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# uv is the project's package manager (gateway/uv.lock). Install it if the base
# image does not already ship it, then make it available on PATH for this shell.
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
if [ -f "$HOME/.local/bin/env" ]; then
  # shellcheck disable=SC1091
  . "$HOME/.local/bin/env"
fi
export PATH="$HOME/.local/bin:$PATH"

# The single shared virtualenv, pinned by gateway/uv.lock.
#   --extra dbt     duckdb + dbt-core/dbt-duckdb (the world builder + gateway)
#   --extra native  the vendor-shaped fake-service deps used by native mode
cd "$repo_root/gateway"
uv sync --extra dbt --extra native

echo "Alkera-DE environment ready: $(uv run python --version)"
