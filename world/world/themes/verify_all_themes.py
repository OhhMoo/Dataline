#!/usr/bin/env python3
"""Clean-seed smoke for the twelve enterprise world themes.

Builds ``seed.example.yaml`` twice, compares ``semantic_hash``, advances one
copy by 96 hours, and checks scheduler job status plus publication receipts.
A theme passes only when that advance appends at least four receipts.

The gate's crash criterion is *zero job crashes on the clean seed*: a theme
may ship its ``live.faults`` catalogue enabled (sftp_vendor's flaky vendor
feed is the point of that theme; the seeded ``sftp_connection_reset`` retry
and ``vendor_late_delivery`` arrival faults record designed ``failed`` runs
on deterministic draws). For every theme, the verifier therefore disables
each catalogue fault the theme declares enabled - through the seed file's
own ``faults:`` override channel, which ``faults.catalogue`` merges over the
theme's ``live.faults`` list - writing the effective seed under the output
root and building both copies from it. Themes with no enabled faults build
their stock ``seed.example.yaml`` untouched. Disabled fault ids are recorded
per theme in the report.

    PYTHONPATH=/home/ubuntu/env_project/world:/home/ubuntu/env_project \\
      /home/ubuntu/env_project/gateway/.venv/bin/python \\
      world/world/themes/verify_all_themes.py [--out ROOT] [--themes name,name]

Worlds are written under ``ROOT/<theme>/{a,b}``. The default root is
``/home/ubuntu/alkera_factory_1017/theme_smoke``. If that directory is already
in use by ``smoke_new_themes``, ``dbt``, or another verifier, this process
writes to ``/home/ubuntu/alkera_factory_1017/theme_smoke_verify`` instead of
deleting the in-flight build. A built world is never placed inside the
env_project repository.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[3]
_WORLD_PARENT = _REPO / "world"
for _entry in (str(_WORLD_PARENT), str(_REPO)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from world.build import build
from world.fingerprint import semantic_hash
from world.ledger import ABSENT
from world.scheduler import advance

THEMES = (
    "commerce_analytics",
    "revenue_daily",
    "sftp_vendor",
    "mainframe_batch",
    "excel_close",
    "settlement_clearing",
    "saas_subscription_mrr",
    "ad_attribution_events",
    "supply_chain_inventory",
    "healthcare_claims_edi",
    "fintech_kyc_risk_limits",
    "iot_telemetry_billing",
)
THEMES_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = Path("/home/ubuntu/alkera_factory_1017/theme_smoke")
BUSY_OUT = Path("/home/ubuntu/alkera_factory_1017/theme_smoke_verify")
ADVANCE_HOURS = 96
MIN_RECEIPTS = 4
_CRASH_MARKERS = ("fail", "error", "crash")


def _foreign_cmdlines() -> list[str]:
    me = os.getpid()
    lines = []
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        lines.append(raw.replace(b"\x00", b" ").decode("utf-8", "replace"))
    return lines


def _mentions_smoke_dir(line: str) -> bool:
    """True when a command uses theme_smoke and not only theme_smoke_verify."""
    stripped = line.replace("theme_smoke_verify", "")
    return "theme_smoke" in stripped


def theme_smoke_busy(lines: list[str] | None = None) -> bool:
    """Another smoke_new_themes or dbt process is using theme_smoke."""
    lines = _foreign_cmdlines() if lines is None else lines
    for line in lines:
        if not _mentions_smoke_dir(line):
            continue
        if "smoke_new_themes" in line or "dbt" in line or "verify_all_themes" in line:
            return True
    return False


def _path_in_use(path: Path, lines: list[str]) -> bool:
    needle = str(path)
    return any(needle in line for line in lines)


def resolve_out(requested: Path | None, lines: list[str]) -> tuple[Path, str]:
    """Pick an output root that is outside the repo and not an in-flight build."""
    if requested is None:
        root, reason = DEFAULT_OUT, "default"
    else:
        root, reason = requested, "cli"
    root = root.expanduser()
    if _inside_repo(root):
        raise SystemExit(f"refusing to build a world inside {_REPO}: {root}")
    busy = theme_smoke_busy(lines)
    defaultish = root.resolve() == DEFAULT_OUT.resolve()
    if busy and defaultish:
        if _inside_repo(BUSY_OUT):
            raise SystemExit(f"refusing to build a world inside {_REPO}: {BUSY_OUT}")
        return BUSY_OUT, "theme_smoke busy; redirected to theme_smoke_verify"
    return root, reason


def _inside_repo(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    repo = _REPO.resolve()
    return resolved == repo or repo in resolved.parents


def declared_publications(theme_dir: Path) -> tuple[list[dict], str]:
    """Publication targets as declared in theme.yaml, plus why receipts exist.

    ``world.ledger.publishes`` reads ``jobs.<name>.publishes`` (or the same
    list under ``exec``). ``world.scheduler._publish`` appends
    ``var/ledger/receipts.jsonl`` only for that list, and only after an
    occurrence actually ran. A theme whose jobs omit ``publishes`` therefore
    does not emit a ledger by design.
    """
    manifest_path = theme_dir / "theme.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    found = []
    for job_name, job in (manifest.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        buckets = [job.get("publishes"), (job.get("exec") or {}).get("publishes")]
        for bucket in buckets:
            if not bucket:
                continue
            for entry in bucket:
                if isinstance(entry, dict):
                    found.append({"job": job_name, **entry})
    if found:
        reason = (
            f"{manifest_path.name} declares {len(found)} publication target(s) "
            f"on scheduler jobs; world.scheduler._publish appends one receipt "
            f"per target after each occurrence that actually runs"
        )
    else:
        reason = (
            f"{manifest_path.name} jobs declare no `publishes` entries. "
            f"world.ledger.publishes returns an empty list unless a job "
            f"declares targets, and world.scheduler._publish writes "
            f"var/ledger/receipts.jsonl only for that list, so this theme "
            f"does not emit receipts by design"
        )
    return found, reason


def receipt_line_count(root: Path) -> tuple[int, bool, str | None, int]:
    """Return (json_lines, file_exists, error, real_hashes).

    ``real_hashes`` counts receipts whose canonical hash is a table or file
    that was actually there. The absent sentinel is a receipt, but it is not
    a publication of this theme's fact.
    """
    path = root / "var" / "ledger" / "receipts.jsonl"
    if not path.is_file():
        return 0, False, None, 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return 0, True, f"unreadable receipts.jsonl: {exc}", 0
    count = 0
    real = 0
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            return count, True, f"receipt line {number} is not JSON: {exc}", real
        if not isinstance(parsed, dict):
            return count, True, f"receipt line {number} is not a JSON object", real
        count += 1
        if parsed.get("canonical_hash") and parsed.get("canonical_hash") != ABSENT:
            real += 1
    return count, True, None, real


def job_crash_count(events: list[dict]) -> tuple[int, int, list[dict]]:
    runs = [event for event in events if event.get("kind") == "job"]
    crashes = []
    for event in runs:
        status = "" if event.get("status") is None else str(event.get("status"))
        lowered = status.lower()
        if status != "success" or any(marker in lowered for marker in _CRASH_MARKERS):
            error = event.get("error")
            if isinstance(error, str) and len(error) > 500:
                error = error[:500] + "...[truncated]"
            crashes.append({
                "name": event.get("name"),
                "status": status,
                "error": error,
            })
    return len(runs), len(crashes), crashes


def _reset_dir(path: Path, lines: list[str]) -> None:
    if path.exists():
        if _path_in_use(path, lines):
            raise RuntimeError(f"refusing to delete in-use build {path}")
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def clean_seed(theme_dir: Path, out_dir: Path) -> tuple[Path, list[str]]:
    """Seed path with every enabled catalogue fault disabled, plus ids.

    ``faults.catalogue`` merges the seed file's top-level ``faults:`` mapping
    over the theme's ``live.faults`` list and rejects ids the theme does not
    declare, so only ids read from ``theme.yaml`` are overridden. A theme with
    no enabled faults builds its stock seed.example.yaml; the effective seed
    otherwise lands under the output root (never inside the repo).
    """
    seed_path = theme_dir / "seed.example.yaml"
    manifest = yaml.safe_load((theme_dir / "theme.yaml").read_text(encoding="utf-8")) or {}
    declared = (manifest.get("live") or {}).get("faults") or []
    enabled = [f["id"] for f in declared
               if isinstance(f, dict) and f.get("id") and f.get("enabled")]
    if not enabled:
        return seed_path, []
    seed = yaml.safe_load(seed_path.read_text(encoding="utf-8")) or {}
    overrides = seed.setdefault("faults", {})
    for fault_id in enabled:
        overrides.setdefault(fault_id, {})["enabled"] = False
    out_dir.mkdir(parents=True, exist_ok=True)
    effective = out_dir / "seed.clean.yaml"
    effective.write_text(yaml.safe_dump(seed, sort_keys=False), encoding="utf-8")
    return effective, enabled


def verify_theme(name: str, out_root: Path, lines: list[str]) -> dict:
    theme_dir = THEMES_DIR / name
    seed = theme_dir / "seed.example.yaml"
    publications, publication_reason = declared_publications(theme_dir)
    pair = out_root / name
    copy_a = pair / "a"
    copy_b = pair / "b"
    row = {
        "theme": name,
        "status": "fail",
        "hash_equal": None,
        "semantic_hash_a": None,
        "semantic_hash_b": None,
        "job_runs": None,
        "job_crash_count": None,
        "job_crashes": [],
        "receipt_line_count": None,
        "real_receipt_count": None,
        "receipts_file_exists": None,
        "publication_targets": len(publications),
        "publication_reason": publication_reason,
        "output_root": str(pair),
        "faults_disabled": [],
        "error": None,
    }
    if not seed.is_file():
        row["error"] = f"missing seed {seed}"
        return row
    if _path_in_use(copy_a, lines) or _path_in_use(copy_b, lines) or _path_in_use(pair, lines):
        row["error"] = f"refusing to delete in-use build under {pair}"
        return row
    try:
        _reset_dir(copy_a, lines)
        _reset_dir(copy_b, lines)
        seed, disabled = clean_seed(theme_dir, pair)
        row["faults_disabled"] = disabled
        build(seed, copy_a)
        build(seed, copy_b)
        hash_a = semantic_hash(copy_a)
        hash_b = semantic_hash(copy_b)
        row["semantic_hash_a"] = hash_a
        row["semantic_hash_b"] = hash_b
        row["hash_equal"] = hash_a == hash_b
        if hash_a != hash_b:
            row["error"] = "semantic hashes differ across two builds of seed.example.yaml"
            return row
        advanced = advance(copy_a, ADVANCE_HOURS)
        events = advanced.get("events") or []
        runs, crashes, details = job_crash_count(events)
        row["job_runs"] = runs
        row["job_crash_count"] = crashes
        row["job_crashes"] = details
        count, exists, receipt_error, real = receipt_line_count(copy_a)
        row["receipt_line_count"] = count
        row["receipts_file_exists"] = exists
        row["real_receipt_count"] = real
        problems = []
        if crashes:
            problems.append(f"{crashes} scheduler job run(s) with crash/error status")
        if receipt_error:
            problems.append(receipt_error)
        elif count < MIN_RECEIPTS:
            problems.append(
                f"var/ledger/receipts.jsonl has {count} receipt(s) after "
                f"{ADVANCE_HOURS}h advance; need at least {MIN_RECEIPTS}"
            )
        elif real < 1:
            problems.append(
                "receipts were written but every canonical_hash is the "
                "absent-target sentinel; the publication target is not a "
                "table or file this theme built"
            )
        if problems:
            row["error"] = "; ".join(problems)
            return row
        row["status"] = "pass"
        return row
    except (Exception, SystemExit) as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None,
                        help="output root (default: theme_smoke, or theme_smoke_verify if busy)")
    parser.add_argument("--themes", default=",".join(THEMES),
                        help="comma-separated theme names")
    args = parser.parse_args(argv)
    names = [part.strip() for part in args.themes.split(",") if part.strip()]
    unknown = [name for name in names if name not in THEMES]
    if unknown:
        raise SystemExit(f"unknown theme(s): {', '.join(unknown)}")
    lines = _foreign_cmdlines()
    out_root, why = resolve_out(args.out, lines)
    out_root.mkdir(parents=True, exist_ok=True)
    if _inside_repo(out_root):
        raise SystemExit(f"refusing to build a world inside {_REPO}")
    print(f"output_root={out_root} ({why})", flush=True)
    results = []
    for name in names:
        print(f"verifying {name}...", flush=True)
        # Re-read process list so a build that started mid-run is not deleted.
        lines = _foreign_cmdlines()
        row = verify_theme(name, out_root, lines)
        results.append(row)
        print(
            f"  {row['status']} hash_equal={row['hash_equal']} "
            f"crashes={row['job_crash_count']} receipts={row['receipt_line_count']}"
            + (f" error={row['error']}" if row["error"] else ""),
            flush=True,
        )
        per_theme = out_root / "results" / f"{name}.json"
        per_theme.parent.mkdir(parents=True, exist_ok=True)
        per_theme.write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
    report = {
        "output_root": str(out_root),
        "output_root_reason": why,
        "passed": sum(row["status"] == "pass" for row in results),
        "failed": sum(row["status"] != "pass" for row in results),
        "results": results,
    }
    report_path = out_root / "verify_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "failed": report["failed"],
                      "report": str(report_path)}), flush=True)
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
