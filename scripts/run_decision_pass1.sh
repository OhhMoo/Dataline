#!/usr/bin/env bash
# pass@1 on the seven rewritten decision arms: native driver, container topology.
#
#   scripts/run_decision_pass1.sh                 # K=8, opus, concurrency 4
#   scripts/run_decision_pass1.sh --k 1           # one rollout per arm, to look first
#   scripts/run_decision_pass1.sh --tasks "GT001-aov-halving GT006-quality-gate-drops-rows"
#   scripts/run_decision_pass1.sh --k 8 --audit      # + grader audit after each task
#
# --audit runs tasks/harness/judge/grader_audit.py --dedupe on each task's run
# as soon as it is graded: an LLM judge, OFF the score, with the same login the
# subject uses (CLAUDE_CODE_OAUTH_TOKEN). It writes $OUT/_audit/<task>/<run>/ and
# never changes pass@1. JUDGE_K (default 3) samples per check.
#
# Robust to a flaky network and to sleep: a task the harness refuses to start
# (exit 3: the pre-run model probe got no answer) is retried up to RETRIES times
# (default 3), WAIT_S apart (default 600); the whole loop holds a sleep inhibitor.
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
AUDIT=0
TASKS="GT001-aov-halving GT002-orders-flatline GT003-tuesday-figure GT005-assertion-never-ran GT006-quality-gate-drops-rows GT007-ran-long-or-started-late GT008-warehouse-cutover"
ARGS=("$@")          # the re-exec under systemd-inhibit needs them after parsing
while [ $# -gt 0 ]; do
  case "$1" in
    --k) K="$2"; shift 2 ;;
    --tasks) TASKS="$2"; shift 2 ;;
    --audit) AUDIT=1; shift ;;
    -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
# Hold the machine awake for the whole loop: a laptop that suspends mid-episode
# freezes the run for hours (measured 2026-09-26: 9 h on one GT001 episode).
if [ -z "${ALKERA_INHIBITED:-}" ] && command -v systemd-inhibit >/dev/null; then
  export ALKERA_INHIBITED=1
  exec systemd-inhibit --what=sleep:idle --who="alkera $RUN_ID" \
    --why="Opus rollouts in progress" "$0" "${ARGS[@]}"
fi
MODEL="${MODEL:-opus}"
CONCURRENCY="${CONCURRENCY:-4}"
# On disk, not /tmp: Fedora's /tmp is a RAM-backed tmpfs, and eight worlds plus
# the fixture staging (taskspec.STAGE_ROOT, also under TMPDIR) do not fit in it.
OUT="${OUT:-$HOME/alkera-rollouts}"
export TMPDIR="${ALKERA_TMPDIR:-$HOME/.cache/alkera-tmp}"
mkdir -p "$TMPDIR"
RUN_ID="${RUN_ID:-${MODEL}-decision-k${K}-$(date -u +%Y%m%dT%H%M%SZ)}"
export RUN_ID
LOGS="$OUT/_logs/$RUN_ID"
mkdir -p "$LOGS"

"$PY" "$REPO/tasks/harness/linux_preflight.py" || {
  echo "preflight failed; fix the FAIL lines above first" >&2; exit 1; }

echo "run-id $RUN_ID  model $MODEL  K=$K  concurrency $CONCURRENCY"
echo "logs   $LOGS"
# Appended, never truncated: a relaunch under the same RUN_ID keeps earlier rows.
[ -f "$LOGS/summary.tsv" ] || printf 'task\texit\treport\n' >"$LOGS/summary.tsv"
RETRIES="${RETRIES:-3}"
WAIT_S="${WAIT_S:-600}"
JUDGE_K="${JUDGE_K:-3}"
for task in $TASKS; do
  attempt=1
  while :; do
    echo "[$(date -u +%H:%M:%SZ)] $task (attempt $attempt)"
    "$PY" "$REPO/tasks/harness/run_rollouts.py" \
      --task "$task" --variant decision \
      --driver native --topology container --model "$MODEL" \
      --k "$K" --concurrency "$CONCURRENCY" \
      --out "$OUT" --run-id "$RUN_ID" \
      >>"$LOGS/$task.log" 2>&1
    code=$?
    # 3 = refused before any episode ran (no model answer: network, quota).
    [ "$code" -eq 3 ] && [ "$attempt" -lt "$RETRIES" ] || break
    echo "    refused to start (exit 3); retrying in ${WAIT_S}s"
    attempt=$((attempt + 1)); sleep "$WAIT_S"
  done
  printf '%s\t%s\t%s\n' "$task" "$code" "$OUT/$task/$RUN_ID/report.md" >>"$LOGS/summary.tsv"
  echo "    exit $code  ->  $OUT/$task/$RUN_ID/report.md"
  if [ "$AUDIT" = 1 ] && ls "$OUT/$task/$RUN_ID"/r*/rollout.json >/dev/null 2>&1; then
    echo "    grader audit (LLM judge, off the score) ..."
    "$PY" "$REPO/tasks/harness/judge/grader_audit.py" --run "$OUT/$task/$RUN_ID" \
      --dedupe --k "$JUDGE_K" >"$LOGS/$task.audit.log" 2>&1
    echo "    audit exit $? (2 = grader defects found)  ->  $OUT/_audit/$task/$RUN_ID/summary.json"
  fi
done
column -t "$LOGS/summary.tsv"
