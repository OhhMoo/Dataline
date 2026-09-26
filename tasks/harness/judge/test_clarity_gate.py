"""Reject-only clarity gate. No network, no grader, no pass@1.

    python3 -m pytest tasks/harness/judge/test_clarity_gate.py
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tasks.harness.judge import consensus, judge_brief, scan_brief  # noqa: E402

# GT009 vague.md's shape: a coworker hands over the nights, names the
# report keys, and does not state the repair. The schema table is not fenced.
COWORKER = """\
Dev is on a call with the card network all morning and asked me to hand this to you.

We missed three nights of the payments feed last week - their push agent was
down. They have since re-sent everything, and the ticket for the backfill was
closed on Thursday, but the clearing bank is still telling us it was handed
nothing for those days. Have a look and sort it out.

While you are in there: keep the nightly right. Whatever you decide, the bank
has to be able to live with what we send it tonight and every night after, so
leave the pipeline able to do the right thing on its own.

Write up what you changed and why in `final_report.json` at the top of the repo.
It gets read by a script, so keep to these keys and put everything else under
`notes`:

| key | value |
|---|---|
| `gap_days_recovered` | list of dates, or `[]` if none |
| `notes` | free text |
"""

_SCORING_IMPORTS = {"reward", "outcome", "grade"}
_NETWORK_IMPORTS = {
    "anthropic",
    "http",
    "httpx",
    "openai",
    "requests",
    "socket",
    "urllib",
}


def _import_targets(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                targets.append(module)
            for alias in node.names:
                targets.append(f"{module}.{alias.name}" if module else alias.name)
    return targets


def _is_clarity_gate(target: str) -> bool:
    parts = target.split(".")
    if "clarity_gate" in parts:
        return True
    return "harness" in parts and "judge" in parts and (
        parts.index("judge") == parts.index("harness") + 1
    )


def _run_cli(brief: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "tasks.harness.judge.clarity_gate",
            "--brief",
            str(brief),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_leaked_phrase_rejects():
    reasons = scan_brief(
        "Please note that the solution is to republish the closed day."
    )
    assert reasons == ["leaked_solution"]
    assert consensus(["reject" if reasons else "hold"]) == "reject"


def test_fenced_expected_table_rejects():
    brief = """\
Sort out the missed nights.

```expected
| day | amount |
| --- | --- |
| 2026-06-12 | 100 |
```
"""
    assert scan_brief(brief) == ["leaked_solution"]


def test_expected_table_on_the_preceding_line_rejects():
    brief = """\
Expected table:

```
| day | amount |
| --- | --- |
| 2026-06-12 | 100 |
```
"""
    assert scan_brief(brief) == ["leaked_solution"]


def test_unfenced_schema_and_command_fence_hold():
    command = COWORKER + """

Run the following:

```
python pipelines/publish_settlement.py
```
"""
    assert scan_brief(command) == []


def test_gt009_coworker_paragraph_holds():
    assert scan_brief(COWORKER) == []
    assert scan_brief(COWORKER, tests_text="assert True") == []
    assert consensus(["hold", "hold", "hold"]) == "hold"
    assert judge_brief(COWORKER) == "hold"


def test_self_referential_names_the_grader():
    brief = "The unit tests will check that the closed day was republished."
    assert scan_brief(brief) == ["self_referential"]


def test_behaviour_untested_is_conservative():
    brief = "You must republish the closed settlement day through the protocol."
    assert scan_brief(brief) == []
    assert scan_brief(brief, tests_text="") == []
    assert scan_brief(brief, tests_text="   ") == []
    assert scan_brief(
        brief, tests_text="def test_checkpoint():\n    assert True\n"
    ) == ["behaviour_untested"]
    assert scan_brief(brief, tests_text="assert republished") == []
    # A second stated verb that the tests do name keeps the miss visible.
    both = brief + " You must withhold the withdrawn figure."
    assert scan_brief(both, tests_text="assert republished") == [
        "behaviour_untested"
    ]


def test_empty_brief_rejects():
    assert scan_brief("") == ["empty_brief"]
    assert scan_brief("   \n") == ["empty_brief"]


def test_consensus_never_returns_accept():
    samples = (
        [],
        ["hold"],
        ["hold", "hold", "hold"],
        ["reject"],
        ["hold", "reject", "hold"],
        ["accept"],
        ["ACCEPT"],
        ["hold", "accept"],
        ["accept", "reject"],
    )
    for sample in samples:
        assert consensus(sample) in {"hold", "reject"}
        assert consensus(sample) != "accept"


def test_one_reject_vote_rejects():
    assert consensus(["hold", "hold", "reject"]) == "reject"
    assert consensus(["reject", "hold", "hold"]) == "reject"
    assert consensus([]) == "reject"


def test_judge_without_client_holds_and_cannot_accept():
    assert judge_brief("The solution is to delete the rows.") == "hold"
    assert judge_brief("") == "hold"

    def client(text: str) -> str:
        assert "rows" in text
        return "accept"

    assert judge_brief("count the rows", client=client) == "hold"
    assert judge_brief("count the rows", client=lambda _text: "reject") == "reject"
    assert (
        judge_brief("count the rows", client=lambda _text: {"verdict": "reject"})
        == "reject"
    )


def test_package_exports_only_the_public_functions():
    import tasks.harness.judge as gate

    assert set(gate.__all__) == {"consensus", "judge_brief", "scan_brief"}


def test_scoring_modules_do_not_import_the_gate():
    watched = [
        ROOT / "tasks" / "harness" / "reward.py",
        ROOT / "tasks" / "harness" / "outcome.py",
    ]
    watched.extend(
        path
        for path in ROOT.joinpath("tasks").rglob("grade.py")
        if "tests" in path.parts
    )
    assert watched
    for path in watched:
        for target in _import_targets(path):
            assert not _is_clarity_gate(target)


def test_gate_does_not_import_scoring_or_the_network():
    package = Path(__file__).resolve().parent
    for path in (package / "clarity_gate.py", package / "__init__.py"):
        for name in _import_targets(path):
            top = name.split(".", 1)[0]
            assert top not in _SCORING_IMPORTS
            assert top not in _NETWORK_IMPORTS
            assert "reward" not in name.split(".")
            assert "outcome" not in name.split(".")


def test_cli_hold_and_reject(tmp_path: Path):
    brief = tmp_path / "brief.md"
    brief.write_text(COWORKER, encoding="utf-8")
    held = _run_cli(brief)
    assert held.returncode == 0
    payload = json.loads(held.stdout)
    assert set(payload) == {"reasons", "consensus"}
    assert payload == {"reasons": [], "consensus": "hold"}

    brief.write_text(
        "The solution is to republish the closed day.\n",
        encoding="utf-8",
    )
    rejected = _run_cli(brief)
    assert rejected.returncode == 2
    payload = json.loads(rejected.stdout)
    assert payload["reasons"] == ["leaked_solution"]
    assert payload["consensus"] == "reject"


def test_cli_usage_and_missing_file_exit_1(tmp_path: Path):
    usage = subprocess.run(
        [sys.executable, "-m", "tasks.harness.judge.clarity_gate"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert usage.returncode == 1
    missing = _run_cli(tmp_path / "missing.md")
    assert missing.returncode == 1
