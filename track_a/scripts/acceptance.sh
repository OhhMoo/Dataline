#!/usr/bin/env bash
# Track A acceptance battery (split_of_work.md §3).
# Host gates need a python with requirements.lock installed:
#   PYTHON=.venv/Scripts/python.exe ./scripts/acceptance.sh           (Windows)
#   PYTHON=.venv/bin/python ./scripts/acceptance.sh                   (Linux)
# Docker gates run when a docker daemon is reachable; otherwise skipped.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python}"
IMG=track_a-world:0.1.0
COMPOSE="docker compose -f docker/docker-compose.yml -p track_a"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

export MSYS_NO_PATHCONV=1  # Git Bash must not rewrite /world container paths

echo "== gate 1: same seed, two scratch builds -> identical semantic fingerprint"
$PY -m world.build --seed seed.example.yaml --root "$tmp/w1" --lock requirements.lock >/dev/null
$PY -m world.build --seed seed.example.yaml --root "$tmp/w2" --lock requirements.lock >/dev/null
h1="$($PY -m world.fingerprint "$tmp/w1")"
h2="$($PY -m world.fingerprint "$tmp/w2")"
echo "  semantic_hash: $h1"
[ "$h1" = "$h2" ] || { echo "  FAIL: fingerprints differ"; exit 1; }
echo "  PASS (same host; CI reruns cross-host)"

echo "== gate 2: snapshot restore -> identical raw bytes"
$PY -m world.snapshot save "$tmp/w1" "$tmp/world.tar" >/dev/null
$PY -m world.snapshot restore "$tmp/world.tar" "$tmp/wr" >/dev/null
$PY -m world.snapshot verify "$tmp/w1" "$tmp/wr"

if docker info >/dev/null 2>&1; then
  echo "== gate 3: episode-zone egress is impossible"
  docker build -q -f track_a/docker/Dockerfile -t "$IMG" .. >/dev/null
  t0=$SECONDS
  $COMPOSE up -d episode >/dev/null
  until $COMPOSE exec -T episode test -f /world/var/sched/state.json 2>/dev/null; do
    [ $((SECONDS - t0)) -gt 120 ] && { echo "  FAIL: boot timeout"; exit 1; }
    sleep 2
  done
  until $COMPOSE --profile harness run --rm harness \
      python -c "import urllib.request; urllib.request.urlopen('http://episode:8080/health')" \
      >/dev/null 2>&1; do
    [ $((SECONDS - t0)) -gt 120 ] && { echo "  FAIL: gateway timeout"; exit 1; }
    sleep 2
  done
  echo "  boot: $((SECONDS - t0))s (limit 60s)"
  [ $((SECONDS - t0)) -lt 60 ] || { echo "  FAIL: boot too slow"; exit 1; }
  $COMPOSE exec -T episode python /opt/world/egress_test.py
  $COMPOSE --profile harness run --rm harness \
      alkera-mcp-smoke http://episode:8080/mcp/
  $COMPOSE --profile harness run --rm harness \
      alkera-gateway call sql_query --url http://episode:8080 \
      --arg "sql=select count(*) as days from warehouse.fct_revenue"
  size_mb=$(docker image inspect "$IMG" --format '{{.Size}}' | awk '{print int($1/1048576)}')
  echo "  image footprint: ${size_mb}MB (budget ~2GB)"
  [ "$size_mb" -lt 2048 ] || { echo "  FAIL: image too large"; exit 1; }
  $COMPOSE down -v >/dev/null
  echo "  PASS"
else
  echo "== gate 3: SKIPPED (no docker daemon)"
fi

echo "ALL ACCEPTANCE GATES PASSED"
