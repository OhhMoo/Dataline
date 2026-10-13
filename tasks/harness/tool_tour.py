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
box's own discovery files - and what must be true afterwards. The Probe, the truth and
the verdict live in `tool_tour_core.py` (stdlib + duckdb, so Task 11's verifier can ship
it); this module re-exports them and runs probes in the CONTAINER topology:

  - rows: the audit rows the command appended to the episode's actions.jsonl, checked
    as 9.2.2 rows at the world's clock (`bi_e2e_check.validate_row`);
  - destinations: destinations.jsonl and every backend's own `destinations.<name>.jsonl`,
    before vs after, against `agent_under_test.allowed_destinations`;
  - truth: `truth_sql` on the world as it was BUILT, read before the stack comes up; a
    second read after the probe is recorded as `truth_after` / `drift`, never judged.

ONE STACK PER PROFILE. Probes are grouped by (profile, plugs); a probe whose rows land
at teardown (`rows_at="teardown"`) gets a stack of its own. Each group gets a fresh
`commerce_analytics` world (seed 11, the BI001/BI002 seed) built for that profile, one
`NativeContainerEpisode` brought up through `bi_e2e_check.ContainerTopology` - imported,
not copied - its probes run in order, each under `timeout` INSIDE the episode, and the
stack is torn down before the next group starts. A "teardown" probe is judged after
`down()` on every row appended since it started. After teardown the group's whole action
log must be a valid trace (contiguous seq, hashes), every row in it must belong to a
probe, and its folded destinations must hold no violation.

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

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import bi_e2e_check as bi  # noqa: E402 - after the path insert, like the checks it reuses
from tool_tour_core import (  # noqa: E402,F401 - re-exported: `from tool_tour import Probe`
    PLACES,
    ROWS_AT,
    VERDICTS,
    WAREHOUSE_REL,
    Probe,
    RowsAt,
    disallowed,
    final_json,
    normalise_host,
    same_value,
    truth,
    verdict,
)

PROBES_PACKAGE = "tool_tour_probes"
PROBES_DIR = HERE / PROBES_PACKAGE
TOPOLOGIES = ("container",)
WORK_ROOT = Path.home() / "alkera-tour"
EVIDENCE = HERE / "evidence" / "tool_tour_container.json"
TAIL = 800
#: One probe's budget, enforced by coreutils `timeout` inside the episode.
PROBE_TIMEOUT_S = 600
#: `timeout -k`: SIGKILL this long after the SIGTERM a probe ignored.
KILL_AFTER_S = 10
#: The host waits this much longer than the in-episode budget before giving up itself.
HOST_GRACE_S = 30


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
def probe_modules() -> list[str]:
    """Every probe module's name, which is the tool its probes prove."""
    return sorted(p.stem for p in PROBES_DIR.glob("*.py") if not p.stem.startswith("_"))


def load_probes(tools: Sequence[str] | None = None) -> list[Probe]:
    """`PROBES` from every `tool_tour_probes/*.py` (or only the named ones), in module
    order. Refuses, naming the module: an unknown tool, a module without probes, a probe
    of the wrong shape (`Probe.__post_init__`: plugs, expect_tools, rows_at, ...), a probe
    filed under another tool's module, and a duplicate id."""
    names = probe_modules()
    if tools is not None:
        unknown = sorted(set(tools) - set(names))
        if unknown:
            raise ValueError(f"no probe module for {unknown} (known: {', '.join(names)})")
        names = [n for n in names if n in set(tools)]
    probes: list[Probe] = []
    for name in names:
        try:
            module = importlib.import_module(f"{PROBES_PACKAGE}.{name}")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{PROBES_PACKAGE}/{name}.py: {exc}") from exc
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
# what the stack wrote
# --------------------------------------------------------------------------
def read_destinations(audit_dir: Path) -> list[dict] | None:
    """destinations.jsonl plus every backend's own `destinations.<name>.jsonl` (a second
    writer keeps its own file until teardown folds it in), one row per (host, port) with
    the counts SUMMED across files. A row teardown already folded into destinations.jsonl
    verbatim is the same connections twice, and counts once. None when no file exists:
    unobserved, never "no connections"."""
    main = bi.read_destinations(audit_dir)
    extra = sorted(Path(audit_dir).glob("destinations.*.jsonl"))
    if main is None and not extra:
        return None
    rows: dict = {}
    seen: dict = {}
    for source in [main or [], *(bi.read_rows(path) for path in extra)]:
        for row in source:
            key = (row.get("host"), row.get("port"))
            if row in seen.get(key, []):
                continue
            seen.setdefault(key, []).append(row)
            if key in rows:
                rows[key] = {**rows[key], "count": int(rows[key].get("count") or 0)
                             + int(row.get("count") or 0)}
            else:
                rows[key] = dict(row)
    return list(rows.values())


def _clock(world: dict) -> str | None:
    try:
        return json.loads(Path(world["scheduler"]).read_text("utf-8"))["vnow"]
    except (OSError, ValueError, KeyError):
        return None


def world_truths(probes: Sequence[Probe], world_root: str | Path) -> dict[str, tuple]:
    """`{probe id: (fact, error)}` from the world as built - called BEFORE the stack is
    up, so nothing a probe (or an agent) does can make the truth agree with it."""
    out: dict[str, tuple] = {}
    for probe in probes:
        if probe.truth_sql is None:
            out[probe.id] = (None, None)
            continue
        try:
            out[probe.id] = (truth(probe.truth_sql, world_root), None)
        except Exception as exc:  # noqa: BLE001 - a broken truth is a failed check
            out[probe.id] = (None, f"{type(exc).__name__}: {exc}")
    return out


# --------------------------------------------------------------------------
# one stack per profile
# --------------------------------------------------------------------------
GroupKey = tuple[str, tuple, str]


def group_key(probe: Probe) -> GroupKey:
    """(profile, plugs, solo): a "teardown" probe's rows are only attributable to it if
    nothing else ran on that stack, so it is a group of one."""
    return probe.profile, probe.plugs, probe.id if probe.rows_at == "teardown" else ""


def group_dir(key: GroupKey) -> str:
    profile, plugs, solo = key
    return slug("-".join([profile, *(f"{s}-{'-'.join(v)}" for s, v in plugs), solo]))


def world_seed(profile_name: str, plugs: Mapping[str, Sequence[str]]) -> str:
    """The BI001/BI002 seed (`commerce_analytics`, seed 11) built for this profile:
    the discovery layer, the parquet exports when the box has the files slot, and a
    `profile:` record `episode.check_world_profile` accepts - the profile's slots,
    its explicit plugs, and the stack's BI vendor when it has one (da/snowflake ->
    sigma), because a vendor's project must be in the workspace."""
    import episode
    from alkera_gateway.plugs import SLOTS

    profile = episode.environment_profile(profile_name, dict(plugs))
    slots = [s for s in SLOTS if s in profile.slots]
    seed_plugs = {s: sorted(v) for s, v in plugs.items()}
    bi_plugs = sorted(profile.slots.get("bi", ()))
    # The rule check_world_profile applies: a vendor BI must be named in the record.
    if "bi" not in seed_plugs and episode._bi_vendor(bi_plugs):
        seed_plugs["bi"] = bi_plugs
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
    is bi_e2e_check's own, not a copy of it.

    NOT thread-safe: it rebinds module globals. `run()` is sequential (one stack at a
    time) and must stay so while this exists; do not call it from two threads."""
    saved = (bi.seed_text, bi.PROFILE_NAME, bi.PLUGS)
    bi.seed_text = lambda _example: seed
    bi.PROFILE_NAME = profile_name
    bi.PLUGS = {s: list(v) for s, v in plugs.items()}
    try:
        yield
    finally:
        bi.seed_text, bi.PROFILE_NAME, bi.PLUGS = saved


class TourTopology(bi.ContainerTopology):
    """`bi_e2e_check.ContainerTopology` for any profile, under its own project name,
    with every command run under `timeout` inside the episode."""

    def __init__(self, world: dict, work: Path, image: str, profile: str,
                 plugs: Mapping[str, Sequence[str]], seed: str):
        super().__init__(world, work, image)
        self.profile, self.plugs, self.seed = profile, dict(plugs), seed
        self.project = f"alkera-tour-{slug(profile)}-{int(time.time())}"

    def up(self) -> dict:
        with as_profile(self.profile, self.plugs, self.seed):
            return super().up()

    def run(self, command: str, timeout: int = PROBE_TIMEOUT_S) -> bi.Outcome:
        """`ContainerTopology.run` under coreutils `timeout` INSIDE the episode: past its
        budget the probe's process group is killed there (TERM, then KILL after
        KILL_AFTER_S), so it cannot write rows into the next probe's window. The host
        allows HOST_GRACE_S more before it gives up on `docker exec` itself."""
        started = time.monotonic()
        done = self.exec_raw(["timeout", "-k", str(KILL_AFTER_S), str(timeout),
                              "bash", "-c", command],
                             workdir="/workspace", timeout=timeout + HOST_GRACE_S)
        return bi.Outcome(done.returncode, done.stdout, done.stderr,
                          round(time.monotonic() - started, 2))


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


def run_probe(topo, probe: Probe) -> bi.Outcome:
    """`topo.run(probe.command)`; a host-side timeout fails the probe, not the group."""
    try:
        return topo.run(probe.command, timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) \
            else (exc.stdout or "")
        return bi.Outcome(124, out, f"timed out after {exc.timeout}s on the host", 0.0)


@dataclass
class Window:
    """Where the action log, the destinations and the clock stood when a probe began."""

    rows: int
    destinations: list[dict] | None
    clock: str | None


def judge(probe: Probe, start: Window, outcome: bi.Outcome, *, audit_dir: Path,
          world: dict, facts: Mapping[str, tuple], allowed: set[tuple[str, int]],
          known: Mapping[str, str]) -> dict:
    """The verdict on every row appended since `start` (for a "teardown" probe this is
    called after `down()`), against the truth read before the stack came up."""
    rows = bi.read_rows(Path(audit_dir) / "actions.jsonl")[start.rows:]
    dest = bi.destination_delta(start.destinations, read_destinations(Path(audit_dir)))
    clocks = tuple(c for c in dict.fromkeys((start.clock, _clock(world))) if c)
    fact, fact_error = facts.get(probe.id, (None, "no truth was read before the run"))
    result = verdict(probe, returncode=outcome.returncode, stdout=outcome.stdout,
                     rows=rows, destinations=dest, allowed=allowed, truth=fact,
                     truth_error=fact_error,
                     validate_row=lambda r: bi.validate_row(r, vnow=clocks or None))
    after, drift = None, None
    if probe.truth_sql is not None and fact_error is None:
        try:
            after = truth(probe.truth_sql, world["world_root"])
            drift = not same_value(after, fact)
        except Exception as exc:  # noqa: BLE001 - recorded, never judged
            after = f"{type(exc).__name__}: {exc}"
    result.update({"truth_after": after, "drift": drift, "seconds": outcome.seconds,
                   "command": probe.command, "known": known.get(probe.id),
                   "stdout_tail": outcome.stdout[-TAIL:],
                   "stderr_tail": outcome.stderr[-TAIL:]})
    return result


def drive(topo, probes: Sequence[Probe], *, world: dict, facts: Mapping[str, tuple],
          allowed: set[tuple[str, int]], known: Mapping[str, str],
          results: list[dict]) -> dict:
    """Run `probes` on an up stack, tear it down, judge, and check the whole episode.

    "call" probes are judged as they return; "teardown" probes after `topo.down()`, on
    every row appended since they began. Results are appended to `results` as they are
    judged, so a failure part-way keeps what was measured. Returns `episode_checks`."""
    audit_dir = Path(topo.audit_dir)
    audit = audit_dir / "actions.jsonl"
    pending = []
    for probe in probes:
        start = Window(len(bi.read_rows(audit)), read_destinations(audit_dir), _clock(world))
        outcome = run_probe(topo, probe)
        if probe.rows_at == "teardown":
            pending.append((probe, start, outcome))
            print(f"  ...   {probe.id:<44} ran; judged after teardown", flush=True)
            continue
        results.append(judge(probe, start, outcome, audit_dir=audit_dir, world=world,
                             facts=facts, allowed=allowed, known=known))
        _print(results[-1])
    topo.down()
    for probe, start, outcome in pending:
        results.append(judge(probe, start, outcome, audit_dir=audit_dir, world=world,
                             facts=facts, allowed=allowed, known=known))
        _print(results[-1])
    return episode_checks(audit_dir, results, allowed)


def _print(result: dict) -> None:
    print(f"  {result['verdict']:<5} {result['id']:<44} rows {result['rows']:<3} "
          f"value {result['value']!r} truth {result['truth']!r}", flush=True)


def run_group(probes: Sequence[Probe], work: Path, image: str,
              known: Mapping[str, str]) -> tuple[dict, list[dict]]:
    """One group's stack: build the world, read every truth, bring the stack up, drive
    the probes, tear down. Any failure - a world build that exits, a stack that never
    comes up - is the group's, recorded, and the tour goes on."""
    import agent_under_test as aut
    import container_episode as ce
    import episode

    first = probes[0]
    profile_name, plugs = first.profile, first.plug_map()
    started = time.time()
    group: dict = {"profile": profile_name, "plugs": {s: list(v) for s, v in plugs.items()},
                   "image": image, "work": str(work), "probes": [p.id for p in probes],
                   "rows_at": first.rows_at}
    results: list[dict] = []
    topo = None
    try:
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        profile = episode.environment_profile(profile_name, plugs)
        # stale_episode_image fails the group: a stale image measures old code
        group["image_gate"] = ce.assert_image_current(image)
        seed = world_seed(profile_name, plugs)
        group["seed"] = seed
        print(f"\n  [{profile_name}] world build under {work}", flush=True)
        with as_profile(profile_name, plugs, seed):
            world = bi.build_world(work)
        episode.check_world_profile(Path(world["world_root"]), profile)
        group["world"] = world
        facts = world_truths(probes, world["world_root"])
        group["truths_read"] = "after the world build, before the stack came up"
        topo = TourTopology(world, work, image, profile_name, plugs, seed)
        print(f"  [{profile_name}] stack up ({image}, project {topo.project})", flush=True)
        up = topo.up()
        group["stack"] = {k: up.get(k) for k in ("container_id", "image", "image_digest",
                                                 "services", "episode_endpoints",
                                                 "subject_env", "workspace_cli")}
        allowed = aut.allowed_destinations(topo.stack.episode_endpoints())
        group["allowed_destinations"] = sorted(f"{h}:{p}" for h, p in allowed)
        group["episode"] = drive(topo, probes, world=world, facts=facts, allowed=allowed,
                                 known=known, results=results)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - the group fails, the tour goes on
        group["error"] = f"{type(exc).__name__}: {exc}"
        print(f"  [{profile_name}] ERROR {group['error'][:600]}", flush=True)
    finally:
        if topo is not None:
            try:
                topo.down()   # a no-op once drive() has torn it down
            except Exception as exc:  # noqa: BLE001
                group.setdefault("error", f"teardown: {type(exc).__name__}: {exc}")
    done = {r["id"] for r in results}
    for probe in probes:
        if probe.id not in done:
            results.append({"id": probe.id, "tool": probe.tool, "profile": probe.profile,
                            "rows_at": probe.rows_at, "verdict": "FAIL",
                            "known": known.get(probe.id),
                            "failures": [f"not run: {group.get('error', 'stack error')}"],
                            "rows": 0, "tools": [], "destinations": {}, "value": None,
                            "truth": None, "value_ok": None, "doc": None,
                            "expect_tools": list(probe.expect_tools), "command": probe.command})
    group["ok"] = "error" not in group and bool((group.get("episode") or {}).get("ok"))
    group["seconds"] = round(time.time() - started, 1)
    return group, results


def episode_checks(audit_dir: Path, results: Sequence[dict],
                   allowed: set[tuple[str, int]] | None) -> dict:
    """After teardown: the whole trace is valid, every row in it is some probe's, and
    the folded destinations hold no violation of the allowlist."""
    import agent_under_test as aut
    import blindness
    from alkera_gateway.audit import validate_trace

    out: dict = {}
    audit = Path(audit_dir) / "actions.jsonl"
    try:
        if audit.exists():
            validate_trace(audit)
        out["trace_valid"] = True
    except Exception as exc:  # noqa: BLE001
        out["trace_valid"] = f"{type(exc).__name__}: {exc}"
    out["rows_total"] = len(bi.read_rows(audit))
    out["rows_in_probes"] = sum(r.get("rows", 0) for r in results)
    out["no_stray_rows"] = out["rows_total"] == out["rows_in_probes"]
    destinations = read_destinations(Path(audit_dir))
    out["destinations"] = destinations
    out["network_violations"] = [str(v) for v in blindness.network_violations(
        destinations, allowed, aut.normalise_host)]
    out["ok"] = (out["trace_valid"] is True and out["no_stray_rows"]
                 and not out["network_violations"])
    return out


def run(probes: Sequence[Probe], *, topology: str,
        image: str | Mapping[str, str] | None, work: Path | None = None) -> dict:
    """Run `probes`, one stack per group (`group_key`), one stack at a time.

    `image`: one image for every profile, `{profile: image}`, or None for each
    profile's own image (`image_for`). Returns the evidence dict (`--out`)."""
    if topology not in TOPOLOGIES:
        raise ValueError(f"topology {topology!r}: the tour runs on {TOPOLOGIES}")
    work = Path(work) if work is not None else WORK_ROOT / topology
    known = known_findings(sorted({p.tool for p in probes}))
    groups: dict[GroupKey, list[Probe]] = {}
    for probe in probes:
        groups.setdefault(group_key(probe), []).append(probe)
    started = time.time()
    evidence: dict = {"topology": topology, "revision": bi._git_rev(), "started": started,
                      "tools": sorted({p.tool for p in probes}), "profiles": [],
                      "probes": []}
    for key, members in groups.items():
        group, results = run_group(members, work / group_dir(key),
                                   image_for(key[0], members[0].plug_map(), image), known)
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
    raise SystemExit(main())
