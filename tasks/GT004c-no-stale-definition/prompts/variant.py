#!/usr/bin/env python3
"""GT004 prompt-variant control. Select one before a run; identify it after one.

The difficulty dial for GT004 is the prompt, not the problem. Three variants
describe the SAME world and are graded by the SAME rubric, so a pass rate that
does not name its variant is not a measurement of anything. This script is how
the variant is chosen and, more importantly, how it is RECOVERED from a finished
run - because a label somebody typed into a report is not evidence.

    python variant.py list
    python variant.py lock                     # recompute the [prompt_variants] block
    python variant.py select guided            # point task.toml at prompts/guided.md
    python variant.py identify <run-or-rollout-dir>

`identify` is the load-bearing one. `tasks/harness/agent_under_test.py` writes
the subject's ENTIRE prompt to <rollout>/prompt.txt, unconditionally, for every
driver, before the episode starts (agent_under_test.py: `run()` ->
`(rollout_dir / "prompt.txt").write_text(prompt, ...)`), and `build_prompt`
appends the task instruction verbatim as the prompt's SUFFIX. So the variant a
rollout actually ran is derivable from the rollout's own stored evidence, with
no cooperation from whoever ran it and no trust in whatever they wrote down.

`identify` fails loudly rather than guessing. A prompt.txt that matches no
locked variant means the prompt was edited between the lock and the run, and
that rollout's score cannot be attributed to a variant at all.

Nothing here is imported by the harness. The harness has exactly one instruction
source - `[agent_visible].brief` in task.toml - so selection is a one-line edit
to that key, which this script makes atomic, hash-checked and stamped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK_ROOT = HERE.parent
TASK_TOML = TASK_ROOT / "task.toml"
STAMP = HERE / "ACTIVE_VARIANT.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def spec() -> dict:
    raw = tomllib.loads(TASK_TOML.read_text(encoding="utf-8"))
    pv = raw.get("prompt_variants")
    if not pv:
        sys.exit("task.toml has no [prompt_variants] block")
    return {"raw": raw, "pv": pv}


def variant_files() -> dict[str, Path]:
    return {p.stem: p for p in sorted(HERE.glob("*.md"))}


def cmd_list(_args) -> int:
    s = spec()
    active = s["raw"].get("agent_visible", {}).get("brief")
    for name, path in variant_files().items():
        locked = (s["pv"].get(name) or {}).get("sha256")
        now = sha256(path)
        mark = "*" if active == f"prompts/{path.name}" else " "
        state = "locked" if locked == now else (
            "NOT LOCKED" if locked is None else "DRIFTED from lock")
        print(f" {mark} {name:<10} {now[:16]}  {state}")
    print(f"\nactive brief: {active}")
    return 0


def cmd_lock(_args) -> int:
    """Print the [prompt_variants] block for the files as they are on disk."""
    print("[prompt_variants]")
    for name, path in variant_files().items():
        print(f'{name} = {{ file = "prompts/{path.name}", '
              f'sha256 = "{sha256(path)}" }}')
    return 0


def cmd_select(args) -> int:
    s = spec()
    name = args.variant
    files = variant_files()
    if name not in files:
        sys.exit(f"unknown variant {name!r}; have: {', '.join(files)}")
    path = files[name]
    locked = (s["pv"].get(name) or {}).get("sha256")
    digest = sha256(path)
    if locked is None:
        sys.exit(f"{name} is not locked in task.toml; run `variant.py lock`")
    if locked != digest and not args.force:
        sys.exit(f"{name} has drifted from its lock in task.toml\n"
                 f"  locked: {locked}\n  on disk: {digest}\n"
                 f"Re-lock deliberately (variant.py lock) - a prompt that "
                 f"changed under a frozen lock invalidates every rollout "
                 f"already attributed to it.")

    text = TASK_TOML.read_text(encoding="utf-8")
    new, n = re.subn(r'(?m)^(brief\s*=\s*)"prompts/[^"]+"',
                     lambda m: m.group(1) + f'"prompts/{path.name}"', text)
    if n != 1:
        sys.exit(f"expected exactly one `brief = \"prompts/...\"` line in "
                 f"task.toml, found {n}")
    with TASK_TOML.open("w", encoding="utf-8", newline="") as fh:
        fh.write(new)

    stamp = {"task": s["raw"]["task"]["id"], "variant": name,
             "file": f"prompts/{path.name}", "sha256": digest,
             "selected_utc": datetime.now(timezone.utc)
                             .strftime("%Y-%m-%dT%H:%M:%SZ")}
    with STAMP.open("w", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(stamp, indent=2) + "\n")
    print(json.dumps(stamp, indent=2))
    return 0


def _prompt_files(root: Path) -> list[Path]:
    direct = root / "prompt.txt"
    if direct.is_file():
        return [direct]
    return sorted(root.glob("*/prompt.txt")) or sorted(root.rglob("prompt.txt"))


def cmd_identify(args) -> int:
    """Derive each rollout's variant from the prompt the subject actually got."""
    bodies = {name: path.read_text(encoding="utf-8")
              for name, path in variant_files().items()}
    root = Path(args.dir).resolve()
    prompts = _prompt_files(root)
    if not prompts:
        sys.exit(f"no prompt.txt under {root}")

    rows, unattributed = [], 0
    for p in prompts:
        text = p.read_text(encoding="utf-8")
        hits = [n for n, body in bodies.items()
                if text.endswith(body) or body.strip() in text]
        row = {"rollout": p.parent.name, "prompt": str(p)}
        if len(hits) == 1:
            row["variant"] = hits[0]
        else:
            row["variant"] = None
            row["why"] = ("matched no locked variant - the prompt was edited "
                          "or suffixed" if not hits else
                          f"ambiguous: matched {hits} - the variants are not "
                          f"distinguishable by text, which is a bug in them")
            unattributed += 1
        rows.append(row)

    print(json.dumps({"dir": str(root), "rollouts": len(rows),
                      "unattributed": unattributed, "rows": rows}, indent=2))
    if unattributed:
        print(f"\nFAIL: {unattributed} rollout(s) cannot be attributed to a "
              f"prompt variant. Their scores must not be aggregated into a "
              f"pass rate.", file=sys.stderr)
        return 1
    seen = sorted({r["variant"] for r in rows})
    if len(seen) > 1:
        print(f"\nFAIL: this run mixes variants {seen}. One pass rate over "
              f"mixed variants is not a number about anything.", file=sys.stderr)
        return 1

    # The harness identifies a run by task + run_id and nothing else
    # (report.py: render(task, run_id, ...)), so the run_id is where the variant
    # has to be written for the report to carry it. Cross-check it against the
    # prompt the subject actually got: a report titled `guided-...` over
    # vague prompts is worse than an unlabelled one.
    report = root / "report.json"
    if report.is_file():
        run_id = json.loads(report.read_text(encoding="utf-8")).get("run_id", "")
        if not str(run_id).startswith(seen[0]):
            print(f"\nFAIL: run_id {run_id!r} does not start with the variant "
                  f"its rollouts actually ran ({seen[0]!r}). The report's title "
                  f"is the only place the variant reaches a reader.",
                  file=sys.stderr)
            return 1
        print(f"  run_id {run_id!r} agrees with the prompts on disk.")

    print(f"\nOK: all {len(rows)} rollout(s) ran prompt variant '{seen[0]}'.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("lock").set_defaults(fn=cmd_lock)
    s = sub.add_parser("select")
    s.add_argument("variant")
    s.add_argument("--force", action="store_true",
                   help="select despite a drifted lock (records nothing new)")
    s.set_defaults(fn=cmd_select)
    i = sub.add_parser("identify")
    i.add_argument("dir", help="a rollout dir, or a run dir holding many")
    i.set_defaults(fn=cmd_identify)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
