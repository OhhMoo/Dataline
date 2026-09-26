#!/usr/bin/env python3
"""Report hidden-blocker substrings in the agent brief.

The brief is prompts/*.md, the file task.toml's `brief` line points at.
task.toml comments are grader metadata: they are printed and never fail
the gate. enabled false documents hits and exits 0. Exit 1 only when the
blocker is enforced and a forbidden substring is in the brief.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml


def scan(text: str, forbidden: list[str], *, enforce: bool) -> dict:
    hits = [s for s in forbidden if s and s in text]
    return {"hits": hits, "ok": (not hits) or (not enforce)}


def brief_text(task_dir: Path) -> str:
    prompts = task_dir / "prompts"
    parts = [
        path.read_text(encoding="utf-8")
        for path in sorted(prompts.glob("*.md"))
    ]
    return "\n".join(parts)


def _print_hits(label: str, hits: list[str]) -> None:
    if hits:
        print(f"{label}:")
        for hit in hits:
            print(f"- {hit}")
    else:
        print(f"{label}: []")


def main() -> int:
    here = Path(__file__).resolve().parent
    task_dir = here.parent
    flags = yaml.safe_load((here / "flags.yaml").read_text(encoding="utf-8"))
    forbidden = list(flags["leakage_forbidden_substrings"])
    enforce = bool(flags["hidden_blocker"]["enabled"])
    brief = scan(brief_text(task_dir), forbidden, enforce=enforce)
    metadata = scan(
        (task_dir / "task.toml").read_text(encoding="utf-8"),
        forbidden,
        enforce=False,
    )
    print(f"enforce: {enforce}")
    _print_hits("prompt_hits", brief["hits"])
    _print_hits("metadata_hits", metadata["hits"])
    print(f"ok: {brief['ok']}")
    if enforce and brief["hits"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
