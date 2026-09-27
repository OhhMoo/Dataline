# scripts/ — host setup and the measurement run

Two entry points, both Linux, both run from anywhere (they resolve the repo from their own
path). Each prints its full usage with `--help`.

```
setup_linux.sh          set up a host: OS packages and a container engine (Docker CE or rootless
                        podman), uv + gateway/.venv from gateway/uv.lock, the claude CLI, the
                        pinned images plus alkera-episode:head and alkera-subject:head, then
                        tasks/harness/linux_preflight.py. Idempotent; re-run after a pull.
                        --engine docker|podman  --skip-packages  --skip-image

run_decision_pass1.sh   pass@1 on the decision arms of GT001-GT003 and GT005-GT008: native driver,
                        container topology, K=8, opus, concurrency 4. Runs the preflight first,
                        retries a task the harness refuses to start (exit 3), holds a sleep
                        inhibitor. --k N  --tasks "GT00x ..."  --audit (off-score grader audit)
                        Env: MODEL CONCURRENCY OUT ALKERA_TMPDIR RUN_ID RETRIES WAIT_S JUDGE_K
                        Results: $OUT/<task>/<RUN_ID>/report.md, $OUT/_logs/<RUN_ID>/summary.tsv
```

Neither creates a subject credential: run `claude setup-token` and export
`CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`). See `tasks/harness/LINUX.md`.

The former status scanner `track_status.py` (and its baseline and OVERVIEW) measured the
tree against the old three-track plan in `docs/implementation_plan/agent.md` §11; it is
archived in `_archive/scripts/`.
