#!/usr/bin/env python3
"""
GT005 prompt-variant tool. The variant is the difficulty dial, so a rollout that
cannot name which ticket produced it is a rate attributed to nothing.

    python variant.py list                  # the three, with drift against the lock
    python variant.py select guided         # rewrite [agent_visible].brief
    python variant.py lock                  # reprint the [prompt_variants] block
    python variant.py identify <run-dir>    # which variant a finished rollout used

`identify` reads the rollout's OWN prompt.txt, not the current file: a prompt
that changed under a frozen lock invalidates every rollout already attributed to
it, and the only way to notice is to hash what was actually shipped.
"""
from __future__ import annotations

import hashlib
import re
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
TOML = TASK / "task.toml"
NAMES = ("vague", "guided", "specific")


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def locked() -> dict:
    raw = tomllib.loads(TOML.read_text(encoding="utf-8"))
    return {name: value["sha256"]
            for name, value in (raw.get("prompt_variants") or {}).items()
            if isinstance(value, dict)}


def current_brief() -> str:
    raw = tomllib.loads(TOML.read_text(encoding="utf-8"))
    return raw.get("agent_visible", {}).get("brief", "")


def cmd_list() -> int:
    lock, brief = locked(), current_brief()
    for name in NAMES:
        path = HERE / f"{name}.md"
        digest = sha(path)
        want = lock.get(name, "")
        state = ("unlocked" if want in ("", "PENDING_LOCK")
                 else "ok" if want == digest else "DRIFTED")
        mark = "*" if brief.endswith(f"{name}.md") else " "
        print(f" {mark} {name:9s} {digest[:12]}  {state}")
    print("\n * is the variant [agent_visible].brief currently points at")
    return 0


def cmd_select(name: str) -> int:
    if name not in NAMES:
        raise SystemExit(f"no such variant: {name} (have {', '.join(NAMES)})")
    text = TOML.read_text(encoding="utf-8")
    new, count = re.subn(r'(?m)^(brief\s*=\s*)"[^"]*"$',
                         lambda m: f'{m.group(1)}"prompts/{name}.md"', text)
    if count != 1:
        raise SystemExit(f"expected exactly one `brief = ...` line, found {count}")
    with open(TOML, "w", encoding="utf-8", newline="") as handle:
        handle.write(new)
    print(f"[agent_visible].brief = prompts/{name}.md")
    return 0


def cmd_lock() -> int:
    print("[prompt_variants]")
    print(f'default  = "{current_brief().rsplit("/", 1)[-1].removesuffix(".md")}"')
    for name in sorted(NAMES):
        print(f'{name:8s} = {{ file = "prompts/{name}.md", '
              f'sha256 = "{sha(HERE / f"{name}.md")}" }}')
    return 0


def cmd_identify(run_dir: str) -> int:
    lock = locked()
    by_digest = {v: k for k, v in lock.items()}
    found = 0
    for prompt in sorted(Path(run_dir).rglob("prompt.txt")):
        digest = sha(prompt)
        print(f"  {by_digest.get(digest, 'UNLOCKED ' + digest[:12]):10s} {prompt}")
        found += 1
    if not found:
        print(f"  no prompt.txt under {run_dir}")
        return 1
    return 0


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        return cmd_list()
    command, rest = argv[0], argv[1:]
    if command == "list":
        return cmd_list()
    if command == "select":
        return cmd_select(*rest)
    if command == "lock":
        return cmd_lock()
    if command == "identify":
        return cmd_identify(*rest)
    raise SystemExit(f"unknown command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
