"""Generate and validate the 12 x 3 standalone governance bundles."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tasks.factory.compiler.blueprint_matrix import THEMES
from tasks.factory.governance.twin_state_machine import MODES, compile_twin, decision_valid
from tasks.factory.narrative.corecraft_synthesizer import write_narrative
from tasks.factory.narrative.discoverability_and_spoiler_linter import lint_task


def verify(out: Path) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "world"))
    from world import workflow

    rows = []
    for theme in THEMES:
        for mode in MODES:
            task = out / theme / mode
            bundle = compile_twin(theme, mode, 91)
            write_narrative(task, bundle, theme)
            lint = lint_task(task)
            spec = {"dir": "fixtures/workflow", "actor": "agent",
                    "project": {"key": "FAC", "name": "Factory"},
                    "statuses": ["approved", "in_progress", "wontfix", "draft", "review"],
                    "workspace": "factory", "space": {"key": "FAC", "name": "Factory"}}
            workflow.documents(task, spec, "2026-10-18T09:00:00Z")
            rows.append({"theme": theme, "mode": mode,
                         "ok": lint.ok and decision_valid(bundle),
                         "coverage": lint.coverage, "spoilers": list(lint.spoilers),
                         "missing": list(lint.missing)})
    report = {"total": len(rows), "passed": sum(r["ok"] for r in rows), "rows": rows}
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path,
                        default=Path("/home/ubuntu/alkera_factory_1017/governance_smoke"))
    args = parser.parse_args()
    report = verify(args.out)
    print(json.dumps({"total": report["total"], "passed": report["passed"]}))
    return 0 if report["total"] == report["passed"] == 36 else 1


if __name__ == "__main__":
    raise SystemExit(main())
