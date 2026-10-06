#!/usr/bin/env bash
# One live rollout of each BI task, watched in tmux: BI001 (guided brief) then
# BI002, native driver, container topology, K=1.
#
#   scripts/live_bi.sh
#
# Run it INSIDE the tmux pane where CLAUDE_CODE_OAUTH_TOKEN (or
# ANTHROPIC_API_KEY) is exported: panes tmux opens take the server's
# environment, not this shell's, so only this pane runs the subject. The
# script splits the window:
#   left         : the run (preflight, episode, grade, report path)
#   right-top    : the agent's own words (») and shell commands ($), from transcript.jsonl
#   right-bottom : every call that reached Power BI / Tableau / Sigma / mf / dbt / tabcmd,
#                  from the gateway's audit log ([error] when the service refused it)
#
#   BI001 - Tableau publish + Sigma write; Power BI and the dbt Semantic Layer
#           (metrics.yml, `mf query`) are what the builds reconcile against.
#   BI002 - Power BI write: updateDefinition + dataset refresh.
#
# The guided brief is selected with BI001's prompts/variant.py, which rewrites
# [agent_visible].brief in task.toml; the script restores it however the run
# ends, and refuses to start if task.toml already has local changes.
#
# Env: OUT (default ~/alkera-rollouts), MODEL (default: run_rollouts.py's).
# Results: "$OUT/<task>/<run-id>/report.md", printed when both are graded.
set -u
SELF=$(readlink -f "$0")
RUN=$(cd "$(dirname "$SELF")/.." && pwd)
OUT=${OUT:-$HOME/alkera-rollouts}
MARK=$OUT/.live_bi_start
PY=$RUN/gateway/.venv/bin/python
BI001=BI001-revenue-disagrees-across-bi
BI002=BI002-powerbi-share-and-stale-refresh

# Follow the newest r0 created since the run started; switch when BI002 begins.
follow() {
  local file=$1 filter=$2 cur="" new
  echo "waiting for the episode to start ..."
  while :; do
    new=$(find "$OUT/$BI001" "$OUT/$BI002" -mindepth 2 -maxdepth 2 -type d -name r0 \
            -newer "$MARK" 2>/dev/null | xargs -r ls -td | head -1)
    if [ -n "$new" ] && [ "$new" != "$cur" ] && [ -f "$new/$file" ]; then
      cur=$new
      printf '\n===== %s =====\n' "$(basename "$(dirname "$(dirname "$cur")")")"
      tail -n +1 -F "$cur/$file" 2>/dev/null | jq -r --unbuffered "$filter" &
    fi
    sleep 3
  done
}

case "${1:-run}" in
  feed)
    follow transcript.jsonl '
      if .type == "assistant" then
        .message.content[]?
        | if .type == "tool_use" then "$ " + (((.input.command // "") | split("\n")[0])[0:160])
          elif .type == "text" then "» " + ((.text | gsub("\n"; " "))[0:240])
          else empty end
      elif .type == "result" then "##### DONE: " + ((.result // "") | gsub("\n"; " "))[0:300]
      else empty end' ;;
  calls)
    follow audit/actions.jsonl '
      select(.tool | test("^(powerbi|tableau|sigma)\\.|^shim\\.(mf|dbt|tabcmd)$"))
      | select(.tool | test("health") | not)
      | "\(.tool)  \((.args_summary // "" | tostring)[0:120])"
        + (if .exit != "ok" then "  [" + (.exit | tostring) + "]" else "" end)' ;;
  run)
    [ -n "${TMUX:-}" ] || { echo "run this inside tmux"; exit 1; }
    [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}" ] || {
      echo "export CLAUDE_CODE_OAUTH_TOKEN in this pane first"; exit 1; }
    command -v jq >/dev/null || { echo "jq is required for the live panes"; exit 1; }
    git -C "$RUN" diff --quiet -- "tasks/$BI001/task.toml" || {
      echo "tasks/$BI001/task.toml has local changes; commit or restore it first"; exit 1; }
    mkdir -p "$OUT" && touch "$MARK"
    model=${MODEL:+--model $MODEL}
    tmux split-window -h -l 55% "bash $SELF feed"
    tmux split-window -v "bash $SELF calls"
    tmux select-pane -L
    cd "$RUN" || exit 1
    trap 'git -C "$RUN" checkout -- "tasks/$BI001/task.toml"' EXIT
    "$PY" "tasks/$BI001/prompts/variant.py" select guided
    sg docker -c "$PY tasks/harness/run_rollouts.py --task $BI001 --variant default --k 1 $model --out $OUT"
    git -C "$RUN" checkout -- "tasks/$BI001/task.toml"
    sg docker -c "$PY tasks/harness/run_rollouts.py --task $BI002 --variant default --k 1 $model --out $OUT"
    echo; echo "reports:"
    for t in $BI001 $BI002; do ls -td "$OUT/$t"/*/ | head -1 | sed 's|$|report.md|'; done ;;
esac
