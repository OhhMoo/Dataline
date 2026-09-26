#!/usr/bin/env bash
# pass@1 on the seven rewritten decision arms: native driver, container topology.
#
#   scripts/run_decision_pass1.sh                 # K=8, opus, concurrency 4
#   scripts/run_decision_pass1.sh --k 1           # one rollout per arm, to look first
#   scripts/run_decision_pass1.sh --tasks "GT001-aov-halving GT006-quality-gate-drops-rows"
#
# Env: MODEL (default opus), CONCURRENCY (default 4; each rollout is a full
# compose stack, ~2 GB), OUT (default ~/alkera-rollouts),
# ALKERA_TMPDIR (default ~/.cache/alkera-tmp), RUN_ID.
#
# Run it in tmux or screen, from a plain terminal: an episode lasts up to two
# hours, and a subject started from inside a Claude Code session inherits that
# session (the harness strips it, but there is no reason to rely on that).
#
# Monitor:  tail -f "$OUT/_logs/<RUN_ID>/<task>.log"
#           docker ps --format '{{.Names}}' | grep -c episode
# Results:  "$OUT/<task>/<RUN_ID>/report.md"  (pass@1 with its Wilson interval)
#           "$OUT/_logs/<RUN_ID>/summary.tsv"
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/gateway/.venv/bin/python"
K=8
TASKS="GT001-aov-halving GT002-orders-flatline GT003-tuesday-figure GT005-assertion-never-ran GT006-quality-gate-drops-rows GT007-ran-long-or-started-late GT008-warehouse-cutover"
while [ $# -gt 0 ]; do
  case "$1" in
    --k) K="$2"; shift 2 ;;
    --tasks) TASKS="$2"; shift 2 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
MODEL="${MODEL:-opus}"
CONCURRENCY="${CONCURRENCY:-4}"
# On disk, not /tmp: Fedora's /tmp is a RAM-backed tmpfs, and eight worlds plus
# the fixture staging (taskspec.STAGE_ROOT, also under TMPDIR) do not fit in it.
OUT="${OUT:-$HOME/alkera-rollouts}"
export TMPDIR="${ALKERA_TMPDIR:-$HOME/.cache/alkera-tmp}"
mkdir -p "$TMPDIR"
RUN_ID="${RUN_ID:-${MODEL}-decision-k${K}-$(date -u +%Y%m%dT%H%M%SZ)}"
LOGS="$OUT/_logs/$RUN_ID"
mkdir -p "$LOGS"

"$PY" "$REPO/tasks/harness/linux_preflight.py" || {
  echo "preflight failed; fix the FAIL lines above first" >&2; exit 1; }

echo "run-id $RUN_ID  model $MODEL  K=$K  concurrency $CONCURRENCY"
echo "logs   $LOGS"
printf 'task\texit\treport\n' >"$LOGS/summary.tsv"
for task in $TASKS; do
  echo "[$(date -u +%H:%M:%SZ)] $task"
  "$PY" "$REPO/tasks/harness/run_rollouts.py" \
    --task "$task" --variant decision \
    --driver native --topology container --model "$MODEL" \
    --k "$K" --concurrency "$CONCURRENCY" \
    --out "$OUT" --run-id "$RUN_ID" \
    >"$LOGS/$task.log" 2>&1
  code=$?
  printf '%s\t%s\t%s\n' "$task" "$code" "$OUT/$task/$RUN_ID/report.md" >>"$LOGS/summary.tsv"
  echo "    exit $code  ->  $OUT/$task/$RUN_ID/report.md"
done
column -t "$LOGS/summary.tsv"
