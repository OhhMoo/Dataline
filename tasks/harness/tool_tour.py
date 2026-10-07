#!/usr/bin/env python3
"""The tool tour: one audited probe per tool, each read through that tool's real client.

    sg docker -c "gateway/.venv/bin/python tasks/harness/tool_tour.py --topology container \\
        --out tasks/harness/evidence/tool_tour_container.json"
    ... --tools duckdb,sched                      # only those probe modules
    ... --image da/laptop=alkera-episode:head-da  # per profile (default: the profile's image)

WHAT A PROBE IS. `tool_tour_probes/<tool>.py` holds `PROBES`, a list of `Probe`s. A
probe is one question an engineer would answer with that tool ("February 2026 Net
Revenue", "how many dbt_build runs succeeded"), the bash `command` that answers it the
way an engineer on the box would - the real CLI, SDK or REST client, configured from the
box's own discovery files - and what must be true afterwards:

  (a) the command exits 0 and its LAST stdout line is a JSON object `{"value": ...}`
      (or `{"doc": "<why>"}`, below);
  (b) every `expect_tools` value appears as the `tool` of an audit row the command left
      in the episode's actions.jsonl, and every one of those rows is a valid 9.2.2 row
      (`bi_e2e_check.validate_row`, at the world's clock);
  (c) every connection the command made (destinations.jsonl and any backend's own
      `destinations.<name>.jsonl`, before vs after) is to an address the profile allows
      (`agent_under_test.allowed_destinations` over the episode's endpoint map);
  (d) `value` equals the scalar `truth_sql` returns from the WORLD, read on the host
      (`truth`): DuckDB on the world's warehouse file, read-only, with
      `file_search_path` set to the world root so a world file (`var/workflow/*.json`,
      `var/sched/state.json`) is readable by its world-relative path. No truth_sql, no
      value check - the value is recorded and the verdict rests on (a)-(c).

The verdict is PASS, FAIL, or DOC: a probe whose last line is `{"doc": ...}` documents a
path that is refused by name or coarser by design, and is DOC when (a)-(c) hold.

ONE STACK PER PROFILE. Probes are grouped by (profile, plugs). Each group gets a fresh
`commerce_analytics` world (seed 11, the BI001/BI002 seed) built for that profile, one
`NativeContainerEpisode` brought up through `bi_e2e_check.ContainerTopology` - imported,
not copied - the group's probes run in order, and the stack is torn down before the next
group starts. After teardown the group's whole action log must be a valid trace
(`alkera_gateway.audit.validate_trace`: contiguous seq, hashes) and its folded
destinations must hold no violation.

Evidence: tasks/harness/evidence/tool_tour_container.json (`--out`). Exit 1 on any FAIL.
Task 11 reuses the probes as Harbor tasks: `command` is the oracle's solve.sh,
`instruction` the live agent's brief, `truth_sql` + `expect_tools` the verifier.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import bi_e2e_check as bi  # noqa: E402 - after the path insert, like the checks it reuses

PROBES_PACKAGE = "tool_tour_probes"
PROBES_DIR = HERE / PROBES_PACKAGE
TOPOLOGIES = ("container",)
WORK_ROOT = Path.home() / "alkera-tour"
EVIDENCE = HERE / "evidence" / "tool_tour_container.json"
VERDICTS = ("PASS", "FAIL", "DOC")
#: The world's warehouse file, relative to the world root (`bi_e2e_check.manifest`).
WAREHOUSE_REL = Path("var") / "warehouse" / "warehouse.duckdb"
TAIL = 800


@dataclass(frozen=True)
class Probe:
    """One fact read through one tool's real client. See the module docstring."""

    id: str                        # "<tool>.<client>.<what>"; <tool> is the module's name
    profile: str                   # "da/laptop"
    plugs: Mapping[str, tuple[str, ...]]
    command: str                   # bash, run in the episode as the agent user; last stdout line is JSON {"value": ...}
    expect_tools: tuple[str, ...]  # audit `tool` values that must appear, e.g. ("snowflake.statement",)
    truth_sql: str | None          # DuckDB SQL over the world warehouse; scalar must equal "value"
    instruction: str               # one-paragraph brief for the Harbor live-agent run (Task 11)

    @property
    def tool(self) -> str:
        return self.id.split(".", 1)[0]


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
def probe_modules() -> list[str]:
    """Every probe module's name, which is the tool its probes prove."""
    return sorted(p.stem for p in PROBES_DIR.glob("*.py") if not p.stem.startswith("_"))


def load_probes(tools: Sequence[str] | None = None) -> list[Probe]:
    """`PROBES` from every `tool_tour_probes/*.py` (or only the named ones), in
    module order. Refuses an unknown tool, a module without probes, a probe filed
    under another tool's module, and a duplicate id."""
    names = probe_modules()
    if tools is not None:
        unknown = sorted(set(tools) - set(names))
        if unknown:
            raise ValueError(f"no probe module for {unknown} (known: {', '.join(names)})")
        names = [n for n in names if n in set(tools)]
    probes: list[Probe] = []
    for name in names:
        module = importlib.import_module(f"{PROBES_PACKAGE}.{name}")
        found = list(getattr(module, "PROBES", None) or [])
        if not found:
            raise ValueError(f"{PROBES_PACKAGE}/{name}.py defines no PROBES")
        for probe in found:
            if not isinstance(probe, Probe):
                raise ValueError(f"{PROBES_PACKAGE}/{name}.py: {probe!r} is not a Probe")
            if probe.tool != name:
                raise ValueError(f"probe {probe.id!r} is filed under {name}.py; its id "
                                 f"must start with '{name}.'")
        probes += found
    seen: set[str] = set()
    duplicates = sorted({p.id for p in probes if p.id in seen or seen.add(p.id)})
    if duplicates:
        raise ValueError(f"duplicate probe ids {duplicates}")
    return probes


def known_findings(tools: Sequence[str] | None = None) -> dict[str, str]:
    """`{probe id: cause}` from the modules' optional `KNOWN` mappings: a FAIL whose
    cause is known and owned elsewhere. It does not change a verdict; it explains it."""
    out: dict[str, str] = {}
    for name in tools if tools is not None else probe_modules():
        out.update(getattr(importlib.import_module(f"{PROBES_PACKAGE}.{name}"), "KNOWN",
                           None) or {})
    return out


# --------------------------------------------------------------------------
# the verdict (pure)
# --------------------------------------------------------------------------
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


def same_value(value: Any, truth: Any) -> bool:
    """Numbers compare as numbers (to 6 places, `bi_e2e_check.same_number`), so
    "2361224.00", 2361224 and Decimal("2361224.00") agree; anything else as text."""
    if value is None or truth is None:
        return value is None and truth is None
    if not isinstance(value, bool) and not isinstance(truth, bool) \
            and bi.same_number(value, truth):
        return True
    return str(value) == str(truth)


def disallowed(destinations: Mapping[tuple[str, int], int],
               allowed: set[tuple[str, int]] | None) -> list[str]:
    """`host:port` the command connected to that the allowlist does not hold."""
    import agent_under_test as aut

    if allowed is None:
        return [f"{h}:{p}" for (h, p), n in sorted(destinations.items()) if n > 0]
    return sorted(f"{h}:{p}" for (h, p), n in destinations.items()
                  if n > 0 and (aut.normalise_host(str(h)), int(p)) not in allowed)


def verdict(probe: Probe, *, returncode: int, stdout: str, rows: Sequence[dict],
            destinations: Mapping[tuple[str, int], int],
            allowed: set[tuple[str, int]] | None, truth: Any = None,
            truth_error: str | None = None,
            vnow: str | tuple[str, ...] | None = None) -> dict:
    """One probe's verdict from what it left behind. Pure: every input is passed in."""
    failures: list[str] = []
    if returncode != 0:
        failures.append(f"exit {returncode}")
    final = final_json(stdout)
    value, doc, has_value = None, None, False
    if final is None:
        failures.append("the last stdout line is not a JSON object")
    elif "doc" in final:
        doc = str(final["doc"])
    elif "value" not in final:
        failures.append(f"the final JSON has no `value`: {sorted(final)}")
    else:
        value, has_value = final["value"], True
    tools = [r.get("tool") for r in rows]
    missing = [t for t in probe.expect_tools if t not in tools]
    if missing:
        failures.append(f"no audit row for {missing} (the command left "
                        f"{sorted(set(map(str, tools))) or 'no rows'})")
    schema = {r.get("seq"): problems for r in rows
              if (problems := bi.validate_row(r, vnow=vnow))}
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
            value_ok = same_value(value, truth)
            if not value_ok:
                failures.append(f"value {value!r} != truth {truth!r}")
    return {
        "id": probe.id, "tool": probe.tool, "profile": probe.profile,
        "verdict": "FAIL" if failures else ("DOC" if doc is not None else "PASS"),
        "failures": failures, "doc": doc,
        "value": value, "truth": truth, "value_ok": value_ok,
        "expect_tools": list(probe.expect_tools), "missing_tools": missing,
        "rows": len(rows), "tools": tools,
        "destinations": {f"{h}:{p}": n for (h, p), n in sorted(destinations.items())},
        "returncode": returncode,
    }


# --------------------------------------------------------------------------
# truth, destinations, rows
# --------------------------------------------------------------------------
def truth(sql: str, world_root: str | Path) -> Any:
    """The scalar `sql` returns from the world (see the module docstring, (d))."""
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
    return rows[0][0]


def read_destinations(audit_dir: Path) -> list[dict] | None:
    """destinations.jsonl plus every backend's own `destinations.<name>.jsonl` (a
    second writer keeps its own file until teardown folds it in), keyed by
    (host, port). None when no file exists: unobserved, never "no connections"."""
    main = bi.read_destinations(audit_dir)
    extra = sorted(Path(audit_dir).glob("destinations.*.jsonl"))
    if main is None and not extra:
        return None
    rows = {(r.get("host"), r.get("port")): r for r in main or []}
    for path in extra:
        for row in bi.read_rows(path):
            rows[(row.get("host"), row.get("port"))] = row
    return list(rows.values())


def _clock(world: dict) -> str | None:
    try:
        return json.loads(Path(world["scheduler"]).read_text("utf-8"))["vnow"]
    except (OSError, ValueError, KeyError):
        return None


# --------------------------------------------------------------------------
# one stack per profile
# --------------------------------------------------------------------------
def group_key(probe: Probe) -> tuple[str, tuple[tuple[str, tuple[str, ...]], ...]]:
    return probe.profile, tuple(sorted((s, tuple(v)) for s, v in probe.plugs.items()))


def world_seed(profile_name: str, plugs: Mapping[str, Sequence[str]]) -> str:
    """The BI001/BI002 seed (`commerce_analytics`, seed 11) built for this profile:
    the discovery layer, the parquet exports when the box has the files slot, and a
    `profile:` record `episode.check_world_profile` accepts - the profile's slots,
    its explicit plugs, and the stack's BI vendor when it has one (da/snowflake ->
    sigma), because a vendor's project must be in the workspace."""
    import episode
    from alkera_gateway.plugs import PLUGS, SLOTS

    profile = episode.environment_profile(profile_name, dict(plugs))
    slots = [s for s in SLOTS if s in profile.slots]
    seed_plugs = {s: sorted(v) for s, v in plugs.items()}
    bi_plugs = profile.slots.get("bi", ())
    if "bi" not in seed_plugs and any(PLUGS[p].services and p != "metabase_fake"
                                      for p in bi_plugs):
        seed_plugs["bi"] = sorted(bi_plugs)
    text = bi.SEED_EXAMPLE.read_text(encoding="utf-8").rstrip("\n") + "\nnative: true\n"
    if "files" in slots:
        text += "files: {formats: [parquet]}\n"
    text += f"profile:\n  name: {profile_name}\n  slots: [{', '.join(slots)}]\n"
    if seed_plugs:
        text += "  plugs: {" + ", ".join(
            f"{s}: [{', '.join(v)}]" for s, v in sorted(seed_plugs.items())) + "}\n"
    return text


@contextlib.contextmanager
def as_profile(profile_name: str, plugs: Mapping[str, Sequence[str]],
               seed: str) -> Iterator[None]:
    """Point bi_e2e_check's world build and container topology at this profile.

    `bi_e2e_check.build_world` and `ContainerTopology.up` read three module globals
    (`seed_text`, `PROFILE_NAME`, `PLUGS`): for the duration of the call they name
    this group's profile, so the code that builds the world and brings the stack up
    is bi_e2e_check's own, not a copy of it."""
    saved = (bi.seed_text, bi.PROFILE_NAME, bi.PLUGS)
    bi.seed_text = lambda _example: seed
    bi.PROFILE_NAME = profile_name
    bi.PLUGS = {s: list(v) for s, v in plugs.items()}
    try:
        yield
    finally:
        bi.seed_text, bi.PROFILE_NAME, bi.PLUGS = saved


class TourTopology(bi.ContainerTopology):
    """`bi_e2e_check.ContainerTopology` for any profile, under its own project name."""

    def __init__(self, world: dict, work: Path, image: str, profile: str,
                 plugs: Mapping[str, Sequence[str]], seed: str):
        super().__init__(world, work, image)
        self.profile, self.plugs, self.seed = profile, dict(plugs), seed
        self.project = f"alkera-tour-{slug(profile)}-{int(time.time())}"

    def up(self) -> dict:
        with as_profile(self.profile, self.plugs, self.seed):
            return super().up()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def image_for(profile_name: str, plugs: Mapping[str, Sequence[str]],
              image: str | Mapping[str, str] | None) -> str:
    """An explicit image (one for all, or per profile name), else the profile's own
    (`container_episode.profile_image`: the role image, `-snowflake` for its clients)."""
    import container_episode as ce
    import episode

    if isinstance(image, str):
        return image
    if isinstance(image, Mapping) and profile_name in image:
        return image[profile_name]
    return ce.profile_image(ce.DEFAULT_IMAGE,
                            episode.environment_profile(profile_name, dict(plugs)))


def run_group(probes: Sequence[Probe], work: Path, image: str,
              known: Mapping[str, str]) -> tuple[dict, list[dict]]:
    """One profile's stack: build the world, bring it up, run every probe, tear down."""
    import agent_under_test as aut
    import container_episode as ce
    import episode

    from alkera_gateway.audit import validate_trace

    profile_name, plugs = probes[0].profile, {s: list(v) for s, v in probes[0].plugs.items()}
    started = time.time()
    group: dict = {"profile": profile_name, "plugs": plugs, "image": image,
                   "work": str(work), "probes": [p.id for p in probes]}
    results: list[dict] = []
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    topo, allowed = None, None
    try:
        profile = episode.environment_profile(profile_name, plugs)
        try:
            group["image_gate"] = ce.assert_image_current(image)
        except episode.HarnessError as exc:
            raise RuntimeError(str(exc)) from exc
        seed = world_seed(profile_name, plugs)
        group["seed"] = seed
        print(f"\n  [{profile_name}] world build under {work}", flush=True)
        with as_profile(profile_name, plugs, seed):
            world = bi.build_world(work)
        episode.check_world_profile(Path(world["world_root"]), profile)
        group["world"] = world
        topo = TourTopology(world, work, image, profile_name, plugs, seed)
        print(f"  [{profile_name}] stack up ({image}, project {topo.project})", flush=True)
        up = topo.up()
        group["stack"] = {k: up.get(k) for k in ("container_id", "image", "image_digest",
                                                 "services", "episode_endpoints",
                                                 "subject_env", "workspace_cli")}
        endpoints = topo.stack.episode_endpoints()
        allowed = aut.allowed_destinations(endpoints)
        group["allowed_destinations"] = sorted(f"{h}:{p}" for h, p in allowed)
        audit = topo.audit_dir / "actions.jsonl"
        for probe in probes:
            before_rows = len(bi.read_rows(audit))
            before_dest = read_destinations(topo.audit_dir)
            clock_before = _clock(world)
            outcome = topo.run(probe.command)
            clock_after = _clock(world)
            rows = bi.read_rows(audit)[before_rows:]
            dest = bi.destination_delta(before_dest, read_destinations(topo.audit_dir))
            fact, fact_error = None, None
            if probe.truth_sql is not None:
                try:
                    fact = truth(probe.truth_sql, world["world_root"])
                except Exception as exc:  # noqa: BLE001 - a broken truth is a failed check
                    fact_error = f"{type(exc).__name__}: {exc}"
            clocks = tuple(c for c in dict.fromkeys((clock_before, clock_after)) if c)
            result = verdict(probe, returncode=outcome.returncode, stdout=outcome.stdout,
                             rows=rows, destinations=dest, allowed=allowed, truth=fact,
                             truth_error=fact_error, vnow=clocks or None)
            result.update({"seconds": outcome.seconds, "command": probe.command,
                           "known": known.get(probe.id),
                           "stdout_tail": outcome.stdout[-TAIL:],
                           "stderr_tail": outcome.stderr[-TAIL:]})
            results.append(result)
            print(f"  {result['verdict']:<5} {probe.id:<44} rows {result['rows']:<3} "
                  f"value {result['value']!r} truth {result['truth']!r}", flush=True)
    except Exception as exc:  # noqa: BLE001 - the group fails, the tour goes on
        group["error"] = f"{type(exc).__name__}: {exc}"
        print(f"  [{profile_name}] ERROR {group['error'][:600]}", flush=True)
    finally:
        if topo is not None:
            try:
                topo.down()
            except Exception as exc:  # noqa: BLE001
                group.setdefault("error", f"teardown: {type(exc).__name__}: {exc}")
    done = {r["id"] for r in results}
    for probe in probes:
        if probe.id not in done:
            results.append({"id": probe.id, "tool": probe.tool, "profile": probe.profile,
                            "verdict": "FAIL", "known": known.get(probe.id),
                            "failures": [f"not run: {group.get('error', 'stack error')}"],
                            "rows": 0, "tools": [], "destinations": {}, "value": None,
                            "truth": None, "value_ok": None, "doc": None,
                            "expect_tools": list(probe.expect_tools), "command": probe.command})
    if topo is not None and allowed is not None:
        group["episode"] = episode_checks(topo, results, allowed, validate_trace)
    group["ok"] = "error" not in group and bool((group.get("episode") or {}).get("ok"))
    group["seconds"] = round(time.time() - started, 1)
    return group, results


def episode_checks(topo, results: list[dict], allowed: set[tuple[str, int]],
                   validate_trace) -> dict:
    """After teardown: the whole trace, and the folded destinations against the allowlist."""
    import agent_under_test as aut
    import blindness

    out: dict = {}
    audit = topo.audit_dir / "actions.jsonl"
    try:
        validate_trace(audit)
        out["trace_valid"] = True
    except Exception as exc:  # noqa: BLE001
        out["trace_valid"] = f"{type(exc).__name__}: {exc}"
    rows = bi.read_rows(audit)
    out["rows_total"] = len(rows)
    out["rows_in_probes"] = sum(r.get("rows", 0) for r in results)
    destinations = read_destinations(topo.audit_dir)
    out["destinations"] = destinations
    out["network_violations"] = [str(v) for v in blindness.network_violations(
        destinations, allowed, aut.normalise_host)]
    out["ok"] = out["trace_valid"] is True and not out["network_violations"]
    return out


def run(probes: Sequence[Probe], *, topology: str,
        image: str | Mapping[str, str] | None, work: Path | None = None) -> dict:
    """Run `probes`, one stack per (profile, plugs), one stack at a time.

    `image`: one image for every profile, `{profile: image}`, or None for each
    profile's own image (`image_for`). Returns the evidence dict (`--out`)."""
    if topology not in TOPOLOGIES:
        raise ValueError(f"topology {topology!r}: the tour runs on {TOPOLOGIES}")
    work = Path(work) if work is not None else WORK_ROOT / topology
    known = known_findings(sorted({p.tool for p in probes}))
    groups: dict = {}
    for probe in probes:
        groups.setdefault(group_key(probe), []).append(probe)
    started = time.time()
    evidence: dict = {"topology": topology, "revision": bi._git_rev(), "started": started,
                      "tools": sorted({p.tool for p in probes}), "profiles": [],
                      "probes": []}
    for (profile_name, _), members in groups.items():
        plugs = members[0].plugs
        group, results = run_group(members, work / slug(profile_name + "-" + "-".join(
            f"{s}-{'-'.join(v)}" for s, v in sorted(plugs.items()))),
            image_for(profile_name, plugs, image), known)
        evidence["profiles"].append(group)
        evidence["probes"] += results
    evidence["seconds"] = round(time.time() - started, 1)
    counts = {v: sum(r["verdict"] == v for r in evidence["probes"]) for v in VERDICTS}
    evidence["summary"] = {**counts, "profiles_ok": all(g["ok"] for g in evidence["profiles"])}
    evidence["findings"] = [{"id": r["id"], "verdict": r["verdict"], "cause": r["known"],
                             "failures": r["failures"]}
                            for r in evidence["probes"] if r["verdict"] == "FAIL"]
    evidence["table"] = render_table(evidence["probes"])
    return evidence


def render_table(results: Sequence[dict]) -> str:
    head = f"{'probe':<44}{'profile':<14}{'verdict':<9}{'rows':>5}  value / truth"
    lines = [head, "-" * (len(head) + 30)]
    for r in results:
        value = "doc: " + str(r["doc"])[:40] if r.get("doc") else \
            f"{r.get('value')!r} / {r.get('truth')!r}"
        lines.append(f"{r['id'][:43]:<44}{r['profile'][:13]:<14}{r['verdict']:<9}"
                     f"{r.get('rows', 0):>5}  {value}")
    return "\n".join(lines)


def _images(values: Sequence[str]) -> str | dict[str, str] | None:
    """`--image TAG` (one for all) or `--image PROFILE=TAG` (repeatable)."""
    if not values:
        return None
    if len(values) == 1 and "=" not in values[0]:
        return values[0]
    out = {}
    for value in values:
        name, sep, tag = value.partition("=")
        if not sep or not name or not tag:
            raise SystemExit(f"--image {value!r}: give one TAG, or PROFILE=TAG per profile")
        out[name] = tag
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--topology", choices=TOPOLOGIES, default="container")
    parser.add_argument("--tools", default=None,
                        help="comma-separated probe modules (default: all)")
    parser.add_argument("--out", type=Path, default=EVIDENCE)
    parser.add_argument("--work", type=Path, default=None,
                        help=f"scratch root (default {WORK_ROOT}/<topology>)")
    parser.add_argument("--image", action="append", default=[],
                        help="TAG for every profile, or PROFILE=TAG (repeatable)")
    args = parser.parse_args(argv)
    tools = [t for t in args.tools.split(",") if t] if args.tools else None
    probes = load_probes(tools)
    print(f"\n  tool tour - {len(probes)} probe(s), topology {args.topology}")
    evidence = run(probes, topology=args.topology, image=_images(args.image),
                   work=args.work)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=1, sort_keys=True, default=str) + "\n",
                        encoding="utf-8")
    print("\n" + evidence["table"])
    for r in evidence["probes"]:
        if r["verdict"] == "FAIL":
            cause = f" [known: {r['known']}]" if r.get("known") else ""
            print(f"\n  FAIL {r['id']}{cause}: " + "\n       ".join(r["failures"]))
    for g in evidence["profiles"]:
        episode_view = {k: v for k, v in (g.get("episode") or {}).items()
                        if k != "destinations"}
        print(f"  {g['profile']}: ok={g['ok']} {g.get('error', '')} {episode_view}")
    print(f"\n  {evidence['summary']}  evidence: {args.out}")
    return 1 if evidence["summary"]["FAIL"] or not evidence["summary"]["profiles_ok"] else 0


if __name__ == "__main__":
    # The probe modules import `Probe` from `tool_tour`; run as that module, not
    # as __main__, so there is one Probe class and not two.
    import tool_tour

    raise SystemExit(tool_tour.main())
