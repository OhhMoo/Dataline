"""mainframe_batch theme hooks: landing-zone generation + raw load.

write_landing: one fixed-block CLAIMS.DAT per day partition, cp037-encoded
text fields with COMP-3 packed-decimal amounts per copybooks/CLAIMS.CPY.
One RNG per day keyed on (seed, day_index); amounts are integer cents — no
binary floats anywhere in the pipeline.
load_raw: executes the workspace's own loader/load_extract.py via subprocess,
so the script the agent reads is the code that actually loads raw.
"""
import os
import random
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import yaml

from world import faults, scheduler, workflow
from world.scheduler import iso, parse_vtime

REC_LEN = 31  # CLAIMS.CPY: X(10) + 9(8) + X(8) + S9(7)V99 COMP-3 (5 bytes)


def pack_comp3(cents: int, digits: int = 9) -> bytes:
    """Pack integer cents as signed COMP-3: digit pairs per byte, sign nibble last."""
    nibbles = [int(c) for c in f"{abs(cents):0{digits}d}"]
    if len(nibbles) > digits:
        raise ValueError(f"amount overflows S9(7)V99: {cents}")
    nibbles.append(0x0D if cents < 0 else 0x0C)
    return bytes((nibbles[i] << 4) | nibbles[i + 1]
                 for i in range(0, len(nibbles), 2))


def record_bytes(row: dict) -> bytes:
    rec = (row["claim_id"].encode("cp037")
           + row["claim_date"].encode("cp037")
           + row["cust_id"].encode("cp037")
           + pack_comp3(row["amount_cents"]))
    assert len(rec) == REC_LEN
    return rec


def day_rows(seed: int, day_index: int, day, params: dict) -> list[dict]:
    rng = random.Random(f"{seed}:{day_index}")
    lo_n, hi_n = params["claims_per_day"]
    lo_c, hi_c = params["amount_cents"]
    rows = []
    for i in range(rng.randint(lo_n, hi_n)):
        rows.append({
            "claim_id": f"CL{day_index * 10000 + i:08d}",
            "claim_date": day.strftime("%Y%m%d"),
            "cust_id": f"CU{rng.randrange(params['customers']):06d}",
            "amount_cents": rng.randint(lo_c, hi_c),
        })
    return rows


def write_day(workspace: Path, seed: dict, day_index: int, day) -> dict:
    """One night's extract. Pure in (seed, day_index) - the live-world arrival
    stream and the initial build write identical bytes for day N."""
    d = workspace / "data" / "landing" / "claims_extract" / f"dt={day}"
    d.mkdir(parents=True, exist_ok=True)
    rows = day_rows(seed["seed"], day_index, day, seed)
    with (d / "CLAIMS.DAT").open("wb") as f:
        for row in rows:
            f.write(record_bytes(row))
    return {"partition": f"dt={day}", "records": len(rows)}


def write_landing(workspace: Path, seed: dict) -> None:
    """Write data/landing/claims_extract/dt=YYYY-MM-DD/CLAIMS.DAT per day."""
    start = parse_vtime(seed["vtime_start"])
    for i in range(seed["days"]):
        write_day(workspace, seed, i, (start + timedelta(days=i)).date())
    write_ops_logs(workspace, seed)


# --------------------------------------------------------------------------
# historical ops logs (var/logs)
# --------------------------------------------------------------------------

def _occurrence_runs(spec: dict, at, fault: dict | None, missed: str | None,
                     duration: int) -> list[tuple]:
    """The recorded runs of one job occurrence: (at, duration_s, status, error,
    try_number, max_tries) per attempt, mirroring scheduler.initial_state's
    per-occurrence fault logic so the audit log and the run history describe
    the same night."""
    kind = fault["kind"] if fault else None
    params = fault["params"] if fault else {}
    status = "failed" if missed else "success"
    if kind == "job_timeout":
        timeout_s = int(params.get("timeout_s", duration))
        return [(at, timeout_s, "failed",
                 params.get("error")
                 or (f"job exceeded its {timeout_s}s timeout and was killed "
                     f"(SIGKILL) before it produced any output"), None, None)]
    if kind == "job_retry":
        attempts = max(2, int(params.get("attempts", 2)))
        delay = int(params.get("retry_delay_minutes", 10))
        message = params.get("error") or "connection reset by peer"
        fail_after = int(params.get("fail_after_s", 5))
        runs = []
        for n in range(1, attempts):
            runs.append((at, fail_after, "failed", message, n, attempts))
            at = at + timedelta(minutes=delay)
        runs.append((at, duration, status, missed, attempts, attempts))
        return runs
    if kind == "job_overrun":
        secs = (int(duration * float(params.get("multiplier", 1.0)))
                + int(params.get("extra_minutes", 0)) * 60)
        return [(at, secs, status, missed, None, None)]
    return [(at, duration, status, missed, None, None)]


def write_ops_logs(workspace: Path, seed: dict) -> None:
    """The platform-side record of the seeded backstory: three rotated daemon
    logs under var/logs/, covering every seeded day on the virtual clock.

    - sftp_sync.log       the off-mainframe transfer gateway receiving the
                          nightly CLAIMS.DAT (23:00Z; on an extract_late night
                          the receive holds open until the deferred cut lands)
    - ingest_daemon.log   the batch-window loader's own log: copybook decode of
                          the night's partition(s) and the raw reload totals.
                          An abend night records the S322 and heals on the next
                          night, exactly as the seeded run history records it
    - scheduler_audit.log the platform's audit trail of the night's three job
                          runs (nightly_extract, batch_load, dbt_build),
                          mirroring initial_state's draws: the extract scan
                          counting 0 files on a late night, overruns, abends

    A pure function of the seed: jitter comes from one stream per day,
    `Random(f"{seed}:logs:{day_index}")`, timestamps derive from
    seed['vtime_start'], and the fault nights come from the SAME
    history_catalogue / history_arrivals / job_decision draws the seeded run
    history reads - one decision, three surfaces. Never a wall clock. Two
    builds of one seed write byte-identical logs. Called from write_landing
    (build time) only: the live arrival stream calls write_day directly and
    appends nothing here.
    """
    start = parse_vtime(seed["vtime_start"])
    folder = Path(workspace).parent / "var" / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    manifest = yaml.safe_load(
        (Path(__file__).resolve().parent / "theme.yaml")
        .read_text(encoding="utf-8"))
    jobs = manifest["jobs"]
    catalogue = faults.history_catalogue(manifest, seed)
    ctx = {"faults": catalogue, "fault_seed": seed["seed"]}
    late = faults.history_arrivals(catalogue, seed["seed"], seed["days"])
    rows_per_day = [
        len(day_rows(seed["seed"], i, (start + timedelta(days=i)).date(), seed))
        for i in range(seed["days"])
    ]
    sync, ingest, audit = [], [], []
    owed = []        # day indices landed but not yet loaded into raw
    raw_rows = 0     # rows in raw.claims_extract after the last committed load
    raw_parts = 0    # partitions (claim dates) in raw.claims_extract
    for i in range(seed["days"]):
        rng = random.Random(f"{seed['seed']}:logs:{i}")
        day = (start + timedelta(days=i)).date()
        iso_day = day.isoformat()
        tokens = {"day": iso_day, "day_compact": day.strftime("%Y%m%d")}
        # --- sftp_sync.log: the 23:00Z receive from the LPAR offload job. A
        # late cut holds the receive open until the deferred time.
        recv = start + timedelta(days=i, hours=23)
        if i in late:
            delay = int(late[i]["params"].get("delay_minutes", 60))
            sync.append(
                f"{iso(recv + timedelta(seconds=rng.randrange(20)))} WARN "
                f"xfer-gw[3301]: PMXTRACT for dt={iso_day} not yet cut on "
                f"mf-lpar1 (JES2 backlog); holding receive open")
            recv = recv + timedelta(minutes=delay)
        size = (workspace / "data" / "landing" / "claims_extract"
                / f"dt={iso_day}" / "CLAIMS.DAT").stat().st_size
        at = recv + timedelta(seconds=rng.randrange(5, 40))
        sync.append(
            f"{iso(at)} INFO xfer-gw[3301]: connected mf-lpar1.example.com:22 "
            f"(offload job PMXTRACT)")
        sync.append(
            f"{iso(at + timedelta(seconds=2))} INFO xfer-gw[3301]: stored "
            f"data/landing/claims_extract/dt={iso_day}/CLAIMS.DAT "
            f"records={rows_per_day[i]} bytes={size} rec_len=31 ok")
        # --- the night's job occurrences, drawn exactly as initial_state
        # drew them; both logs below read this one decision.
        starts = scheduler.job_starts(jobs, start + timedelta(days=i))
        arrival = faults.arrival_decision(ctx, i)
        runs = {}
        for name, spec in jobs.items():
            at_j = starts[name]
            fault = faults.job_decision(ctx, name, iso(at_j))
            missed = faults.missed_by(arrival, name, tokens)
            runs[name] = _occurrence_runs(spec, at_j, fault, missed,
                                          int(spec["duration_s"]))
        # --- ingest_daemon.log: the loader's own account of batch_load. The
        # partition an extract_late night withheld is simply not there at
        # 23:45; an abend commits nothing and the next night picks up every
        # partition still owed.
        to_load = owed + ([] if i in late else [i])
        begin, took, status, error = runs["batch_load"][-1][:4]
        end = begin + timedelta(seconds=took)
        ingest.append(
            f"{iso(begin)} INFO batch-load[7412]: load_claims_extract started "
            f"(window closes 02:00Z)")
        if status == "success":
            for j in to_load:
                d_j = (start + timedelta(days=j)).date().isoformat()
                ingest.append(
                    f"{iso(begin + timedelta(seconds=1))} INFO "
                    f"batch-load[7412]: decoded dt={d_j}: {rows_per_day[j]} "
                    f"record(s) via copybooks/CLAIMS.CPY")
            raw_rows += sum(rows_per_day[j] for j in to_load)
            raw_parts += len(to_load)
            owed = [i] if i in late else []
            if took > 120:
                ingest.append(
                    f"{iso(end)} WARN batch-load[7412]: finished OK in "
                    f"{took}s (nominal 120s); overran the batch window")
            else:
                ingest.append(
                    f"{iso(end)} INFO batch-load[7412]: finished OK in "
                    f"{took}s")
            ingest.append(
                f"{iso(end + timedelta(seconds=1))} INFO batch-load[7412]: "
                f"raw.claims_extract reloaded: {raw_rows} row(s) across "
                f"{raw_parts} partition(s)")
        else:
            owed = to_load
            ingest.append(f"{iso(end)} ERROR batch-load[7412]: {error}")
        # --- scheduler_audit.log: the platform's audit trail of all three
        # runs, attempt by attempt, exactly as initial_state recorded them.
        notes = {
            "nightly_extract": f"matched=1 pattern=dt={iso_day}/CLAIMS.DAT",
            "batch_load": f"raw.claims_extract_rows={raw_rows}",
            "dbt_build": f"warehouse.fct_claims_daily_rows={raw_parts}",
        }
        for name, spec in jobs.items():
            detail = (spec["detail"].format(day=iso_day)
                      if spec.get("detail") else None)
            duration = int(spec["duration_s"])
            tag = f" {detail}" if detail else ""
            for begin, took, status, error, try_n, max_n in runs[name]:
                attempt = f" attempt={try_n}/{max_n}" if try_n else ""
                audit.append(
                    f"{iso(begin)} INFO sched-audit[220]: job={name}{tag} "
                    f"started{attempt}")
                end = begin + timedelta(seconds=took)
                if status == "success":
                    level = "WARN" if took > duration else "INFO"
                    note = f" {notes.get(name, '')}".rstrip()
                    extra = (f" overran_window=true nominal_s={duration}"
                             if took > duration else "")
                    audit.append(
                        f"{iso(end)} {level} sched-audit[220]: job={name} "
                        f"status=success duration_s={took}{attempt}"
                        f"{extra}{note}")
                else:
                    level = "WARN" if try_n and try_n < max_n else "ERROR"
                    audit.append(
                        f"{iso(end)} {level} sched-audit[220]: job={name} "
                        f"status=failed duration_s={took}{attempt} "
                        f"error={error}")
    for name, lines in (("ingest_daemon.log", ingest),
                        ("sftp_sync.log", sync),
                        ("scheduler_audit.log", audit)):
        # LF like the landing extracts: var/logs sits beside the fingerprinted
        # planes, so the bytes are the same on every host.
        (folder / name).write_text("".join(f"{line}\n" for line in lines),
                                   encoding="utf-8")


def load_raw(workspace: Path, db_path: Path) -> None:
    script = workspace / "loader" / "load_extract.py"
    landing = workspace / "data" / "landing" / "claims_extract"
    r = subprocess.run(
        [sys.executable, str(script), "--landing", str(landing),
         "--db", str(db_path)],
        env={**os.environ, "PYTHONUTF8": "1"},
        stdin=subprocess.DEVNULL,  # never inherit the MCP JSON-RPC pipe; see executor.py
        capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"load_extract.py failed:\n{r.stdout}\n{r.stderr}")


def write_workflow(root: Path, seed: dict, manifest: dict) -> None:
    """Seed var/workflow/{issues,chat,kb}.json per gateway/SURFACES.md#workflow.

    The corpus is authored as data in this theme's workflow/ directory and
    dated against the world clock, which is read out of the scheduler state
    `build` has already written rather than re-derived from the seed - one
    definition of `vnow`, so a ticket cannot be filed after today.
    """
    workflow.seed(root, Path(__file__).resolve().parent, manifest["workflow"],
                  scheduler.load(root)["vnow"])
