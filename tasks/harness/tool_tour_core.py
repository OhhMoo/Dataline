"""The tool tour's core: the Probe, its truth and its verdict. Stdlib + duckdb only.

Nothing here imports the harness or the gateway, so Task 11's in-container verifier can
ship this one file beside the probes. `tool_tour.py` (the container runner) re-exports
every name, so `from tool_tour import Probe` keeps working for the probe modules.

THE VERDICT (`verdict`) holds a probe to four things:

  (a) the command exits 0 and its LAST stdout line is a JSON object with exactly one of
      `value` (the answer) or `doc` (a path refused by name or coarser by design);
  (b) every `expect_tools` group is met by an audit row the command left: a row whose
      `tool` is one of the group's alternatives AND whose `exit` is "ok" (a failed call
      proves nothing). Over the rows that meet a group, every `expect_args` substring
      appears in the argv of at least one, and every `expect_targets` glob (fnmatch)
      matches a `targets` entry of at least one. Every row passes the caller's 9.2.2
      check (`validate_row`);
  (c) every connection it made is to an allowed (host, port);
  (d) `value` equals `truth` - the scalar `truth_sql` returned from the world BEFORE
      the probe's stack came up (`truth`). A NULL truth is a failed check, never a pass.

PASS when all hold, DOC when they hold and the answer line is `doc`, FAIL otherwise.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal

#: When a probe's audit rows exist: as the command returns ("call"), or only once the
#: stack is torn down ("teardown": a tailer that folds a server log into the action log
#: in `down()`, as Task 9's Postgres tailer does).
RowsAt = Literal["call", "teardown"]
ROWS_AT = ("call", "teardown")
VERDICTS = ("PASS", "FAIL", "DOC")
#: The world's warehouse file, relative to the world root.
WAREHOUSE_REL = Path("var") / "warehouse" / "warehouse.duckdb"
#: Numbers agree to this many decimal places (money is two; a float sum is not exact).
PLACES = 6

Plugs = tuple[tuple[str, tuple[str, ...]], ...]
#: expect_tools as stored: all-of groups, each a tuple of any-of tool names.
Groups = tuple[tuple[str, ...], ...]


def _plugs(value: Any, probe_id: str) -> Plugs:
    """{slot: names} (or the pairs this returns) -> sorted ((slot, (name, ...)), ...).
    A bare string is one plug; anything else is refused by name."""
    if isinstance(value, Mapping):
        items = list(value.items())
    elif isinstance(value, tuple) and all(isinstance(p, tuple) and len(p) == 2
                                          for p in value):
        items = list(value)
    else:
        raise TypeError(f"probe {probe_id!r}: plugs must be a mapping of slot -> plug "
                        f"names, not {type(value).__name__}")
    out = []
    for slot, names in items:
        if not isinstance(slot, str) or not slot:
            raise TypeError(f"probe {probe_id!r}: plug slot {slot!r} is not a string")
        if isinstance(names, str):
            names = (names,)
        if not isinstance(names, (tuple, list)) or not names \
                or not all(isinstance(n, str) and n for n in names):
            raise TypeError(f"probe {probe_id!r}: plugs[{slot!r}] must be a plug name or "
                            f"a non-empty tuple of them, not {names!r}")
        out.append((slot, tuple(names)))
    if len({s for s, _ in out}) != len(out):
        raise ValueError(f"probe {probe_id!r}: a plug slot is named twice")
    return tuple(sorted(out))


def _strings(value: Any, name: str, probe_id: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (tuple, list)) or not all(isinstance(v, str) and v
                                                       for v in value):
        raise TypeError(f"probe {probe_id!r}: {name} must be a tuple of non-empty strings, "
                        f"not {value!r}")
    return tuple(value)


def _groups(value: Any, probe_id: str) -> Groups:
    """expect_tools -> ((tool, ...), ...): each entry one tool (a bare string) or a group
    of alternatives (a tuple or list of them); a bare string is one tool. An empty group
    could never be met, so it is refused, as an empty expect_tools is."""
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"probe {probe_id!r}: expect_tools must be a tuple of tools or "
                        f"groups of tools, not {value!r}")
    if not value:
        raise ValueError(f"probe {probe_id!r}: expect_tools is empty - a probe proves "
                         f"a tool by the audit rows it leaves")
    out = []
    for entry in value:
        if not isinstance(entry, (str, tuple, list)):
            raise TypeError(f"probe {probe_id!r}: expect_tools entry {entry!r} is neither "
                            f"a tool nor a group of tools")
        if not isinstance(entry, str) and not entry:
            raise ValueError(f"probe {probe_id!r}: expect_tools has an empty group of "
                             f"alternatives - no row could ever meet it")
        out.append(_strings(entry, "an expect_tools group", probe_id))
    return tuple(out)


@dataclass(frozen=True)
class Probe:
    """One fact read through one tool's real client.

    Frozen and hashable. `plugs` may be written as a mapping (`{"bi": ("sigma",)}`, a
    bare string is one plug); it is stored as sorted `((slot, (plug, ...)), ...)` pairs,
    and `plug_map()` gives the mapping back."""

    id: str                        # "<tool>.<client>.<what>"; <tool> is the module's name
    profile: str                   # "da/laptop"
    plugs: Plugs                   # written {slot: (plug, ...)}; stored as sorted pairs
    command: str                   # bash, run in the episode as the agent user;
                                   # last stdout line is JSON {"value": ...}
    expect_tools: Groups           # all-of groups of any-of audit `tool` values, each met by
                                   # a row with exit "ok"; written ("snowflake.statement",)
                                   # or (("databricks.table_get", "databricks.tables_list"),)
                                   # and stored as ((tool, ...), ...)
    truth_sql: str | None          # DuckDB SQL over the world warehouse; scalar must equal "value"
    instruction: str               # one-paragraph brief for the Harbor live-agent run (Task 11)
    rows_at: RowsAt = "call"       # "teardown": the rows land in the stack's down()
    expect_args: tuple[str, ...] = field(default=())  # substrings of an expected row's argv
    expect_targets: tuple[str, ...] = field(default=())  # fnmatch globs, all-of, each
                                                        # matching an expected row's target

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or self.id.count(".") < 2:
            raise ValueError(f"probe id {self.id!r} is not '<tool>.<client>.<what>'")
        object.__setattr__(self, "plugs", _plugs(self.plugs, self.id))
        object.__setattr__(self, "expect_tools", _groups(self.expect_tools, self.id))
        for name in ("expect_args", "expect_targets"):
            object.__setattr__(self, name, _strings(getattr(self, name), name, self.id))
        if self.rows_at not in ROWS_AT:
            raise ValueError(f"probe {self.id!r}: rows_at {self.rows_at!r} not in {ROWS_AT}")
        for name in ("profile", "command", "instruction"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"probe {self.id!r}: {name} is empty")
        if self.truth_sql is not None and (not isinstance(self.truth_sql, str)
                                           or not self.truth_sql.strip()):
            raise ValueError(f"probe {self.id!r}: truth_sql must be SQL or None")

    @property
    def tool(self) -> str:
        return self.id.split(".", 1)[0]

    def plug_map(self) -> dict[str, tuple[str, ...]]:
        return dict(self.plugs)


# --------------------------------------------------------------------------- truth
def truth(sql: str, world_root: str | Path) -> Any:
    """The scalar `sql` returns from the world at `world_root`.

    DuckDB opens the world's warehouse file read-only, so the file's catalog is
    `warehouse` and a table is `warehouse.warehouse.<table>`; `file_search_path` is the
    world root, so `read_json('var/workflow/issues.json')` reads the world's own file.
    One row of one non-NULL column, or ValueError.

    The world it reads MUST be the one the agent never touched. The container tour calls
    this right after the world is built, before the stack comes up. Task 11's verifier
    must pass a sealed pre-agent copy of the world, never the live /world: anything the
    agent can write, it can make agree with its own answer.
    """
    import duckdb

    root = Path(world_root)
    con = duckdb.connect(str(root / WAREHOUSE_REL), read_only=True)
    try:
        con.execute("SET file_search_path = '" + root.as_posix().replace("'", "''") + "'")
        rows = con.execute(sql).fetchall()
    finally:
        con.close()
    if len(rows) != 1 or len(rows[0]) != 1:
        raise ValueError(f"truth_sql must return one scalar; it returned {len(rows)} "
                         f"row(s) of {len(rows[0]) if rows else 0} column(s)")
    if rows[0][0] is None:
        raise ValueError("truth_sql returned NULL: there is no fact to compare against")
    return rows[0][0]


# --------------------------------------------------------------------------- the verdict
def final_json(stdout: str) -> dict | None:
    """The JSON object on the last non-empty stdout line, or None."""
    lines = [line for line in (stdout or "").splitlines() if line.strip()]
    if not lines:
        return None
    try:
        found = json.loads(lines[-1])
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def same_value(value: Any, fact: Any) -> bool:
    """Numbers compare as numbers to PLACES decimals, so "2361224.00", 2361224 and
    Decimal("2361224.00") agree; anything else compares as text. None never matches."""
    if value is None or fact is None:
        return False
    a, b = _number(value), _number(fact)
    if a is not None and b is not None:
        return round(a, PLACES) == round(b, PLACES)
    return str(value) == str(fact)


def normalise_host(host: str) -> str:
    """`localhost`, `127.0.0.1` and `::1` are one host (agent_under_test.normalise_host,
    restated so this module imports nothing of the harness; a test keeps them equal)."""
    return "127.0.0.1" if host in ("localhost", "127.0.0.1", "::1") else host


def disallowed(destinations: Mapping[tuple[str, int], int],
               allowed: set[tuple[str, int]] | None) -> list[str]:
    """`host:port` the command connected to (count > 0) that `allowed` does not hold."""
    dialled = [(h, p) for (h, p), n in destinations.items() if n > 0]
    if allowed is None:
        return sorted(f"{h}:{p}" for h, p in dialled)
    return sorted(f"{h}:{p}" for h, p in dialled
                  if (normalise_host(str(h)), int(p)) not in allowed)


def _argv(row: dict) -> str:
    argv = (row.get("args") or {}).get("argv") if isinstance(row.get("args"), dict) else None
    return " ".join(map(str, argv)) if isinstance(argv, list) else ""


def _targets(row: dict) -> list[str]:
    targets = row.get("targets")
    return [str(t) for t in targets] if isinstance(targets, list) else []


def meets(row: dict, group: Sequence[str]) -> bool:
    """A row meets a group when its `tool` is one of the group's alternatives and the
    call succeeded: a `databricks.tables_list` that exited "error" proves nothing."""
    tool = row.get("tool")
    return isinstance(tool, str) and tool in group and row.get("exit") == "ok"


def verdict(probe: Probe, *, returncode: int, stdout: str, rows: Sequence[dict],
            destinations: Mapping[tuple[str, int], int],
            allowed: set[tuple[str, int]] | None, truth: Any = None,
            truth_error: str | None = None,
            validate_row: Callable[[dict], list[str]] | None = None) -> dict:
    """One probe's verdict from what it left behind. Pure: every input is passed in.

    `truth` is the PRE-RUN fact (`truth()` on the untouched world); `validate_row` is the
    caller's 9.2.2 row check (bi_e2e_check.validate_row in the container tour)."""
    fact = truth
    failures: list[str] = []
    if returncode != 0:
        failures.append(f"exit {returncode}")
    final = final_json(stdout)
    value, doc, has_value = None, None, False
    if final is None:
        failures.append("the last stdout line is not a JSON object")
    elif "value" in final and "doc" in final:
        failures.append("the final JSON has both `value` and `doc`: answer, or document "
                        "a refusal, not both")
    elif "doc" in final:
        doc = str(final["doc"])
    elif "value" not in final:
        failures.append(f"the final JSON has no `value`: {sorted(final)}")
    else:
        value, has_value = final["value"], True
    tools = [r.get("tool") for r in rows]
    groups = [list(g) for g in probe.expect_tools]
    missing = [list(g) for g in probe.expect_tools if not any(meets(r, g) for r in rows)]
    if missing:
        failed = sorted({str(r.get("tool")) for r in rows
                         if any(r.get("tool") in g for g in missing)})
        failures.append(f"no audit row for {missing} with exit \"ok\" (the command left "
                        f"{sorted(set(map(str, tools))) or 'no rows'}"
                        + (f"; {failed} did not exit ok" if failed else "") + ")")
    expected = [r for r in rows if any(meets(r, g) for g in probe.expect_tools)]
    argvs = [_argv(r) for r in expected]
    missing_args = [a for a in probe.expect_args if not any(a in argv for argv in argvs)]
    if missing_args:
        failures.append(f"no {groups} row's argv contains {missing_args}")
    targets = sorted({t for r in expected for t in _targets(r)})
    missing_targets = [g for g in probe.expect_targets
                       if not any(fnmatchcase(t, g) for t in targets)]
    if missing_targets:
        failures.append(f"no {groups} row's targets match {missing_targets} (they name "
                        f"{targets[:20] or 'nothing'})")
    if validate_row is not None:
        schema = {r.get("seq"): problems for r in rows if (problems := validate_row(r))}
        if schema:
            failures.append(f"9.2.2: {schema}")
    outside = disallowed(destinations, allowed)
    if outside:
        failures.append(f"connections outside the profile's allowlist: {outside}")
    value_ok = None
    if probe.truth_sql is not None and has_value:
        if truth_error is not None:
            value_ok = False
            failures.append(f"truth_sql failed: {truth_error}")
        else:
            value_ok = same_value(value, fact)
            if not value_ok:
                failures.append(f"value {value!r} != truth {fact!r}")
    return {
        "id": probe.id, "tool": probe.tool, "profile": probe.profile,
        "rows_at": probe.rows_at,
        "verdict": "FAIL" if failures else ("DOC" if doc is not None else "PASS"),
        "failures": failures, "doc": doc,
        "value": value, "truth": fact, "value_ok": value_ok,
        "expect_tools": groups, "missing_tools": missing,
        "expect_args": list(probe.expect_args), "missing_args": missing_args,
        "expect_targets": list(probe.expect_targets), "missing_targets": missing_targets,
        "rows": len(rows), "tools": tools,
        "destinations": {f"{h}:{p}": n for (h, p), n in sorted(destinations.items())},
        "returncode": returncode,
    }
