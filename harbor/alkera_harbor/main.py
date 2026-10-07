"""CLI: generate the Alkera Harbor dataset.

    gateway/.venv/bin/python harbor/export.py --output-dir harbor/dist/alkera-de
    gateway/.venv/bin/python harbor/export.py --output-dir DIR --task-ids GT001-aov-halving:decision
    gateway/.venv/bin/python harbor/export.py --output-dir DIR --overwrite --validate
    gateway/.venv/bin/python harbor/export.py --list

Harbor's adapter flags (`--output-dir`, `--task-ids`, `--limit`, `--overwrite`,
`--list`) behave as in harbor's adapters; the rest are this adapter's own.
Exit status: 0 when every selected arm was generated (or skipped) and, with
`--validate`, loaded by Harbor with an equal digest; 1 otherwise.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .adapter import AlkeraHarborAdapter, ExportError, report_json
from .constants import (AGENT_NETWORK_MODES, DEFAULT_DATASET_NAME,
                        DEFAULT_DATASET_VERSION, REWARD_MODES, TEMPLATE_DIR)
from .naming import NamingError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harbor/export.py",
        description="Turn every (task, arm) of every task package (tasks/GT*, "
                    "tasks/BI*) into a Harbor task directory plus dataset.toml.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--output-dir", type=Path,
                        help="directory to write the dataset into (required unless --list)")
    parser.add_argument("--task-ids", nargs="+", default=None, metavar="ID",
                        help="TASK (all arms), TASK:ARM, or a Harbor task/dir name; "
                             "comma-separated values are accepted too")
    parser.add_argument("--limit", type=int, default=None,
                        help="generate only the first N selected arms")
    parser.add_argument("--overwrite", "--force", action="store_true", dest="overwrite",
                        help="regenerate task directories that already exist")
    parser.add_argument("--list", action="store_true", dest="list_tasks",
                        help="list every exportable (task, arm) and exit")
    parser.add_argument("--reward", choices=REWARD_MODES, default="pass",
                        help="pass: binary 1/0 on the arm's top band (default); "
                             "training: the declared shaped scalar in [-2.5, 1]")
    parser.add_argument("--agent-network", choices=AGENT_NETWORK_MODES,
                        default="no-network",
                        help="[agent].network_mode (default no-network; the operator "
                             "opens the model API with --allow-agent-host)")
    parser.add_argument("--dataset-name", default=DEFAULT_DATASET_NAME,
                        help=f"[dataset].name in dataset.toml (default {DEFAULT_DATASET_NAME})")
    parser.add_argument("--dataset-version", default=DEFAULT_DATASET_VERSION,
                        help=f"[dataset].version (default {DEFAULT_DATASET_VERSION})")
    parser.add_argument("--validate", action="store_true",
                        help="load every generated task with a real Harbor install "
                             "and require its digest to equal ours")
    parser.add_argument("--harbor-python", default=None,
                        help="interpreter with harbor importable (default "
                             "$ALKERA_HARBOR_PYTHON, else the one behind `harbor`)")
    parser.add_argument("--template-dir", type=Path, default=TEMPLATE_DIR,
                        help="template root with environment/ and tests/ "
                             "(default harbor/template)")
    parser.add_argument("--work-dir", type=Path, default=None,
                        help="where worlds are built before re-rooting "
                             "(default: the system temp dir)")
    parser.add_argument("--report", type=Path, default=None,
                        help="also write the per-arm results as JSON here")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.list_tasks and args.output_dir is None:
        build_parser().error("--output-dir is required")
    try:
        adapter = AlkeraHarborAdapter(
            output_dir=args.output_dir or Path("."), limit=args.limit,
            overwrite=args.overwrite, task_ids=args.task_ids,
            reward_mode=args.reward, agent_network=args.agent_network,
            dataset_name=args.dataset_name, dataset_version=args.dataset_version,
            template_dir=args.template_dir, work_dir=args.work_dir,
            validate=args.validate, harbor_python=args.harbor_python)
        if args.list_tasks:
            refs = adapter.selected()
            print(f"{len(refs)} exportable arm(s):")
            for ref in refs:
                info = adapter.describe(ref)
                print(f"  {info['selector']:<48} {info['name']:<52} role={info['role']:<8} "
                      f"oracle={info['oracle']:<7} anchors={info['anchors']:<7} "
                      f"brief={info['brief']}")
            return 0
        report = adapter.run()
    except (ExportError, NamingError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    ok = sum(r.status == "ok" for r in report.results)
    skipped = sum(r.status == "skipped" for r in report.results)
    print(f"done in {report.seconds}s: {ok} generated, {skipped} skipped, "
          f"{len(report.errors)} failed; dataset: {report.dataset_toml}")
    for r in report.errors:
        print(f"  FAILED {r.selector}: {r.reason}", file=sys.stderr)
    for problem in report.dataset_problems:
        print(f"  DATASET: {problem}", file=sys.stderr)
    if args.report:
        args.report.write_text(report_json(report) + "\n", encoding="utf-8")
    return 1 if report.errors or report.dataset_problems else 0


if __name__ == "__main__":
    sys.exit(main())
