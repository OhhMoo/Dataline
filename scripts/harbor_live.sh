#!/usr/bin/env bash
# A live Claude agent over the Harbor datasets, inside Harbor's sandbox (one container per
# task, agent network closed except the model host, separate no-network verifier).
#
#   scripts/harbor_live.sh tour     # the tool tour: one task per tool probe, every tool
#   scripts/harbor_live.sh bi       # BI001 + BI002, both arms (4 tasks)
#   scripts/harbor_live.sh tasks    # every GT and BI arm (32 tasks)
#   scripts/harbor_live.sh all      # tour, then tasks
#   scripts/harbor_live.sh export   # only (re)export the datasets; no model, no token
#
# Run it in tmux, in a pane where the model login is exported:
#   export ANTHROPIC_API_KEY=...                                   or
#   export CLAUDE_CODE_OAUTH_TOKEN=...   (CLAUDE_FORCE_OAUTH=1 is set for you)
#
# Datasets are exported to harbor/dist/ (gitignored) at the current commit and reused on
# the next run; REEXPORT=1 rebuilds them. Each run writes a jobs dir next to its dataset
# and prints a per-task (or per-tool) table at the end.
#
# Env: MODEL (default anthropic/claude-opus-5-5), N (concurrent trials, default 4),
#      HARBOR (the harbor CLI), WORK (scratch on disk, default ~/.cache/alkera-harbor-work),
#      REEXPORT=1.
set -u
SELF=$(readlink -f "$0")
REPO=$(cd "$(dirname "$SELF")/.." && pwd)
PY=$REPO/gateway/.venv/bin/python
HARBOR=${HARBOR:-$HOME/.cache/alkera-harbor-0.23.0/bin/harbor}
export ALKERA_HARBOR_PYTHON=${ALKERA_HARBOR_PYTHON:-$(dirname "$HARBOR")/python}
MODEL=${MODEL:-anthropic/claude-opus-5-5}
N=${N:-4}
WORK=${WORK:-$HOME/.cache/alkera-harbor-work}
DIST=$REPO/harbor/dist
REV=$(git -C "$REPO" rev-parse --short HEAD)
# /tmp is a small tmpfs on this host: world builds and Harbor scratch go to disk.
mkdir -p "$WORK/tmp" && export TMPDIR=$WORK/tmp
cd "$REPO" || exit 1

need_login() {
  [ -n "${ANTHROPIC_API_KEY:-}${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || {
    echo "export ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN in this pane first"; exit 1; }
  [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && export CLAUDE_FORCE_OAUTH=1
  [ -x "$HARBOR" ] || { echo "harbor CLI not found at $HARBOR (set HARBOR=...)"; exit 1; }
}

# export_ds NAME [export.py args...]: export once per commit unless REEXPORT=1.
export_ds() {
  local name=$1; shift
  local out=$DIST/$name stamp=$DIST/$name/.alkera-rev
  if [ -f "$out/dataset.toml" ] && [ "${REEXPORT:-0}" != 1 ] && [ "$(cat "$stamp" 2>/dev/null)" = "$REV" ]; then
    echo "dataset $name: reusing the export at $REV"; return 0
  fi
  echo "dataset $name: exporting at $REV ..."
  "$PY" harbor/export.py "$@" --output-dir "$out" --overwrite --validate \
      --work-dir "$WORK/export-$name" || { echo "export of $name failed"; exit 1; }
  echo "$REV" > "$stamp"
}

# run_ds NAME: the live agent over one dataset; prints the jobs dir on its last line.
run_ds() {
  local name=$1 jobs
  jobs=$DIST/$name-jobs-$(date -u +%Y%m%dT%H%M%SZ)
  echo "running $MODEL over $name (-n $N) -> $jobs"
  sg docker -c "'$HARBOR' run -p '$DIST/$name' -a claude-code -m '$MODEL' \
      --allow-agent-host api.anthropic.com -n $N -o '$jobs'"
  LAST_JOBS=$jobs
}

# summarize JOBS: reward per trial from Harbor's result.json files.
summarize() {
  python3 - "$1" <<'EOF'
import json, sys
from pathlib import Path
rows = []
for f in sorted(Path(sys.argv[1]).rglob("result.json")):
    try:
        d = json.loads(f.read_text())
    except ValueError:
        continue
    if "task_name" not in d:
        continue
    reward = ((d.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    rows.append((d["task_name"], reward))
for name, reward in rows:
    print(f"  {'-' if reward is None else reward:>4}  {name}")
scored = [r for _, r in rows if r is not None]
passed = sum(1 for r in scored if r == 1)
print(f"\n  {passed}/{len(scored)} reward 1 ({len(rows) - len(scored)} with no reward: verifier error)")
EOF
}

BI_IDS="BI001-revenue-disagrees-across-bi:default,BI001-revenue-disagrees-across-bi:decision,BI002-powerbi-share-and-stale-refresh:default,BI002-powerbi-share-and-stale-refresh:decision"

tour() {
  export_ds alkera-tooltour --tooltour
  [ "${1:-}" = export-only ] && return 0
  run_ds alkera-tooltour
  python3 harbor/tooltour_report.py "$LAST_JOBS" --json "harbor/evidence/tooltour-claude-$REV.json"
}
bi()    { export_ds alkera-bi --task-ids "$BI_IDS"; [ "${1:-}" = export-only ] && return 0; run_ds alkera-bi; summarize "$LAST_JOBS"; }
tasks() { export_ds alkera-de;                     [ "${1:-}" = export-only ] && return 0; run_ds alkera-de; summarize "$LAST_JOBS"; }

case "${1:-}" in
  tour)   need_login; tour ;;
  bi)     need_login; bi ;;
  tasks)  need_login; tasks ;;
  all)    need_login; tour; tasks ;;
  export) tour export-only; bi export-only; tasks export-only ;;
  *) sed -n '2,20p' "$SELF"; exit 2 ;;
esac
