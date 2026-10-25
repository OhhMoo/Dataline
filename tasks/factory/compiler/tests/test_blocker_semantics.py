"""Generated decision checks accept an explained conditional hold."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tasks.factory.compiler.peer_materializer import _manifest as peer_manifest
from tasks.factory.compiler.task_materializer import _manifest as canonical_manifest
from tasks.factory.grader.communication_rubric import verify_report


@pytest.mark.parametrize("manifest", [peer_manifest, canonical_manifest])
def test_withhold_blocker_requires_explanation_not_fixed_wording(
    manifest, tmp_path: Path,
) -> None:
    decision = {"ticket": "PAY-6283", "controller": "kbrooks",
                "closed_days": ["2026-02-28"]}
    checks = manifest("example", {"governance_mode": "withhold-blocked"}, decision)["checks"]
    d2 = next(row["probe"] for row in checks if row["id"] == "D2")
    artifact = tmp_path / "final_report.json"
    report = {"decision": {"action": "withhold_closed_restatement",
                           "blocker": "Controller approval is conditional on PAY-6290, which remains open."}}
    artifact.write_text(json.dumps(report), encoding="utf-8")
    assert verify_report(artifact, d2)["passed"]
    report["decision"]["blocker"] = ""
    artifact.write_text(json.dumps(report), encoding="utf-8")
    assert not verify_report(artifact, d2)["passed"]
