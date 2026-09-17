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

from world import scheduler, workflow
from world.scheduler import parse_vtime

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
