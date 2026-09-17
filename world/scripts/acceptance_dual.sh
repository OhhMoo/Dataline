#!/usr/bin/env bash
# Acceptance gates 4-7 (agent.md §9.3 Track A) re-executed against a world
# that runs THREE warehouses - the primary plus `legacy` and `target`
# (world/world/themes/revenue_daily/theme.yaml) - through docker/compose.dual.yml.
#
#   bash scripts/acceptance_dual.sh
#
# scripts/acceptance.sh is untouched and still measures the one-warehouse stack;
# this file is the dual-case measurement of the same properties, plus the two
# that only exist with a second warehouse: every declared name answers through
# the gateway, and the fingerprint the container computes carries the
# `warehouses` section. Requires a docker daemon; there is no fallback because
# the properties under test are properties of the container.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -z "${PYTHON:-}" ]; then
  for c in .venv/Scripts/python.exe .venv/bin/python ../gateway/.venv/Scripts/python.exe python; do
    [ -x "$c" ] || command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c "import duckdb, dbt, yaml" 2>/dev/null && { PYTHON="$c"; break; }
  done
fi
PY="${PYTHON:?no python with requirements.lock deps found (create .venv first)}"
IMG=alkera-world:0.1.0
COMPOSE="docker compose -f docker/docker-compose.yml -f docker/compose.dual.yml -p alkera-dual"
STACK_UP=0
cleanup() {
  [ "$STACK_UP" = 1 ] && $COMPOSE --profile grading down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT
export MSYS_NO_PATHCONV=1
docker info >/dev/null 2>&1 || { echo "no docker daemon: the dual gates cannot run"; exit 1; }

echo "== dual gate 0: image from this tree"
docker build -q -f docker/Dockerfile -t "$IMG" .. >/dev/null
echo "  built $IMG"

echo "== dual gate 4: episode boot <60s with three warehouses; episode-zone egress is impossible"
t0=$SECONDS
$COMPOSE up -d episode >/dev/null
STACK_UP=1
until $COMPOSE exec -T episode test -f /world/var/sched/state.json 2>/dev/null; do
  [ $((SECONDS - t0)) -gt 120 ] && { echo "  FAIL: boot timeout"; $COMPOSE logs episode | tail -30; exit 1; }
  sleep 2
done
until $COMPOSE --profile harness run --rm harness \
    python -c "import urllib.request; urllib.request.urlopen('http://episode:8080/health')" \
    >/dev/null 2>&1; do
  [ $((SECONDS - t0)) -gt 120 ] && { echo "  FAIL: gateway timeout"; $COMPOSE logs episode | tail -30; exit 1; }
  sleep 2
done
echo "  boot: $((SECONDS - t0))s (limit 60s)"
[ $((SECONDS - t0)) -lt 60 ] || { echo "  FAIL: boot too slow"; exit 1; }
$COMPOSE exec -T episode test -f /world/var/warehouse/legacy.duckdb \
  || { echo "  FAIL: legacy.duckdb was not built"; exit 1; }
$COMPOSE exec -T episode test -f /world/var/warehouse/target.duckdb \
  || { echo "  FAIL: target.duckdb was not built"; exit 1; }
$COMPOSE exec -T episode python /opt/world/egress_test.py
echo "  PASS"

echo "== dual gate 5: quiescence - three warehouses, fingerprint stable; the warehouses section is there"
q0="$($COMPOSE exec -T episode python -m world.fingerprint /world | tr -d '\r')"
sections="$($COMPOSE exec -T episode python -c "import json,sys;from world import fingerprint;print(','.join(sorted(fingerprint.semantic('/world'))))" | tr -d '\r')"
echo "  sections: $sections"
case "$sections" in *warehouses*) ;; *) echo "  FAIL: no warehouses section"; exit 1 ;; esac
sleep 5
q1="$($COMPOSE exec -T episode python -m world.fingerprint /world | tr -d '\r')"
[ "$q0" = "$q1" ] || { echo "  FAIL: idle services perturbed the fingerprint"; exit 1; }
echo "  idle 5s: fingerprint stable ($q0)"
echo "  PASS"

echo "== dual gate 6: every declared warehouse answers by name through the gateway; the schema says so"
for w in primary legacy target; do
  out="$($COMPOSE --profile harness run --rm harness \
      alkera-gateway call sql_query --url http://episode:8080 \
      --arg "sql=select count(*) as n from raw.daily_revenue" --arg "warehouse=$w" | tr -d '\r')"
  echo "  $w: $(printf '%s' "$out" | tr -d '\n' | cut -c1-120)"
  printf '%s' "$out" | grep -q '"ok": *true' || { echo "  FAIL: $w did not answer"; exit 1; }
done
join="$($COMPOSE --profile harness run --rm harness \
    alkera-gateway call sql_query --url http://episode:8080 \
    --arg "sql=select count(*) as agreeing from wh.warehouse.fct_revenue p join legacy.warehouse.fct_revenue l using (order_date) where p.revenue = l.revenue" | tr -d '\r')"
echo "  duckdb<->duckdb join: $(printf '%s' "$join" | tr -d '\n ' | cut -c1-100)"
printf '%s' "$join" | tr -d '\n ' | grep -q '"rows":\[\[30\]\]' || { echo "  FAIL: the cross-warehouse join did not agree on 30 days"; exit 1; }
# `|| true`: the CLI exits non-zero on a refused call, and the refusal is
# exactly what this line is asserting; under `set -e` the substitution would
# otherwise end the script silently
ro="$($COMPOSE --profile harness run --rm harness \
    alkera-gateway call sql_execute --url http://episode:8080 \
    --arg "sql=delete from warehouse.fct_revenue" --arg "warehouse=legacy" 2>&1 | tr -d '\r' || true)"
printf '%s' "$ro" | grep -q 'warehouse_read_only' || { echo "  FAIL: a write to the read-only legacy warehouse was not refused"; exit 1; }
echo "  write to legacy by name: refused (warehouse_read_only)"
enum="$($COMPOSE exec -T episode python -c "import json;from alkera_gateway.config import GatewayConfig;from alkera_gateway.warehouses import build_tools;print(json.dumps(build_tools(GatewayConfig.load(__import__('pathlib').Path('/etc/alkera/gateway.json')))['sql_query'][0].model_json_schema()['properties']['warehouse']['enum']))" | tr -d '\r')"
echo "  sql_query.warehouse enum: $enum"
q2="$($COMPOSE exec -T episode python -m world.fingerprint /world | tr -d '\r')"
[ "$q0" = "$q2" ] || echo "  WARNING: the queries above perturbed the fingerprint ($q0 -> $q2)"
echo "  PASS"

echo "== dual gate 7: /grading unreachability - the grader does not exist during the episode"
$COMPOSE exec -T episode test ! -e /grading \
  || { echo "  FAIL: /grading exists inside the episode container"; exit 1; }
$COMPOSE --profile harness run --rm harness test ! -e /grading \
  || { echo "  FAIL: /grading exists inside a harness run"; exit 1; }
if $COMPOSE ps -a --format json | tr -d '\r' | grep -q grading; then
  echo "  FAIL: a grading container is present while the episode is live"; exit 1
fi
echo "  episode live: no /grading in episode or harness; no grading container"
$COMPOSE --profile grading up -d grading >/dev/null
cid="$($COMPOSE --profile grading ps -q grading | tr -d '\r')"
nm="$(docker inspect "$cid" --format '{{.HostConfig.NetworkMode}}' | tr -d '\r')"
[ "$nm" = "none" ] || { echo "  FAIL: grading NetworkMode is '$nm', expected 'none'"; exit 1; }
$COMPOSE exec -T episode test ! -e /grading \
  || { echo "  FAIL: /grading appeared inside episode after grading boot"; exit 1; }
if $COMPOSE exec -T episode python -c "import socket; socket.getaddrinfo('grading', 443)" 2>/dev/null; then
  echo "  FAIL: episode can resolve/reach the 'grading' hostname"; exit 1
fi
# the grader sees all three warehouse files, read-only
$COMPOSE --profile grading exec -T grading sh -c 'ls /world/var/warehouse/ && ! touch /world/var/warehouse/legacy.duckdb' \
  || { echo "  FAIL: the grader could write the world"; exit 1; }
echo "  grading up: NetworkMode none; episode still has no /grading; 'grading' unresolvable; world read-only to the grader"
$COMPOSE --profile grading down -v >/dev/null
STACK_UP=0
echo "  PASS"

echo "ALL DUAL GATES PASSED (4, 5, 6, 7 against three warehouses)"
