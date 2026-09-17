# scripts/ — repository status tooling

**Purpose.** Re-derive the project's track status from the tree instead of from
prose. `track_status.py` runs both test suites, resolves imports in the venv, reads
the evidence files the gates write (`world/evidence/acceptance.json`,
`gateway/docker/evidence/*.json`) and prints one table: what is done with executed
evidence, what merely exists, and what is incomplete (stubs, skipped tests,
truncated files, unresolvable imports).

```
track_status.py             python scripts/track_status.py [--no-tests] [--json] [--baseline F] [--save-baseline F]
track_status_baseline.json  the last full run; --baseline diffs against it
```

`done` is never awarded for a file existing. Spec it measures against: `agent.md` §11.
