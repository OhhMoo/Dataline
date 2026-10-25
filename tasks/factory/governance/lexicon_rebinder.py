"""Deterministic rebinding of task-local names, tickets, and calendar dates.

The compiler owns this mapping. We replace exact tokens in structured values,
never arbitrary substrings, so that table names and prose stay internally
consistent across workflow artifacts and grading metadata.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import random
import re
from collections.abc import Mapping
from typing import Any

HANDLES = (
    "alane", "bchen", "cpatel", "dortiz", "emorgan", "fkim", "gward",
    "hross", "isingh", "jrivera", "kbrooks", "lperry", "mnguyen", "ncole",
)
PROJECTS = ("PAY", "FIN", "CLM", "INV", "REV", "RISK", "OPS", "DATA")
DATE_RE = re.compile(r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)")
TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,7}-\d{2,7}\b")
HANDLE_RE = re.compile(r"(?<![A-Za-z0-9_])@[a-z][a-z0-9_]{2,20}\b")


def _seed_int(seed: int | str) -> int:
    digest = hashlib.sha256(str(seed).encode()).digest()
    return int.from_bytes(digest[:8], "big")


def make_lexicon(seed: int | str, *, theme: str = "", aliases: Mapping[str, str] | None = None) -> dict:
    """Return a stable per-task lexicon. The date shift is a whole number of days."""
    rng = random.Random(_seed_int(f"{theme}:{seed}"))
    handles = list(HANDLES)
    rng.shuffle(handles)
    project = PROJECTS[rng.randrange(len(PROJECTS))]
    number = rng.randrange(1000, 8800)
    # Restrict shifts to keep demo dates close to the base seed's era.
    shift = rng.randrange(-84, 85)
    return {
        "seed": str(seed), "theme": theme, "project": project,
        "ticket_start": number, "day_shift": shift,
        "roles": {
            "controller": handles[0], "engineering_lead": handles[1],
            "operator": handles[2], "reporter": handles[3],
            "risk_officer": handles[4], "agent": "agent",
        },
        "aliases": dict(aliases or {}),
    }


def ticket_ids(lexicon: Mapping[str, Any], count: int = 8) -> list[str]:
    return [f"{lexicon['project']}-{lexicon['ticket_start'] + i}" for i in range(count)]


def shift_date(value: str, days: int) -> str:
    def replace(match: re.Match[str]) -> str:
        try:
            return (dt.date.fromisoformat(match.group(1)) + dt.timedelta(days=days)).isoformat()
        except ValueError:
            return match.group(1)
    return DATE_RE.sub(replace, value)


def rebind(value: Any, lexicon: Mapping[str, Any], *, tickets: Mapping[str, str] | None = None,
           handles: Mapping[str, str] | None = None) -> Any:
    """Recursively rebind dates, exact ticket IDs, @handles, and configured aliases."""
    tickets = tickets or {}
    handles = handles or {}
    aliases = lexicon.get("aliases") or {}

    def string(s: str) -> str:
        s = shift_date(s, int(lexicon["day_shift"]))
        s = TICKET_RE.sub(lambda m: tickets.get(m.group(), m.group()), s)
        s = HANDLE_RE.sub(lambda m: "@" + handles.get(m.group()[1:], m.group()[1:]), s)
        # Whole-word alias replacement prevents a short metric name from
        # changing a longer column name by accident.
        for before, after in sorted(aliases.items(), key=lambda p: -len(p[0])):
            s = re.sub(rf"(?<![\w]){re.escape(before)}(?![\w])", after, s)
        return s

    if isinstance(value, str):
        return string(value)
    if isinstance(value, list):
        return [rebind(v, lexicon, tickets=tickets, handles=handles) for v in value]
    if isinstance(value, tuple):
        return tuple(rebind(v, lexicon, tickets=tickets, handles=handles) for v in value)
    if isinstance(value, dict):
        return {string(str(k)): rebind(v, lexicon, tickets=tickets, handles=handles)
                for k, v in value.items()}
    return copy.deepcopy(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", required=True)
    parser.add_argument("--theme", default="")
    args = parser.parse_args()
    print(json.dumps(make_lexicon(args.seed, theme=args.theme), indent=2))


if __name__ == "__main__":
    main()
