"""Batch-window loader: mainframe claims extract -> raw.claims_extract.

Reads every dt=* partition under the landing root, decodes the fixed-block
cp037 records per copybooks/CLAIMS.CPY, unpacks the COMP-3 amount, and loads
raw.claims_extract. PRAGMA threads=1 + ORDER BY so the warehouse page layout
is reproducible run over run.
"""
import argparse
import sys
from decimal import Decimal
from pathlib import Path

import duckdb

REC_LEN = 31
CLAIM_ID = (0, 10)
CLAIM_DATE = (10, 18)
CUST_ID = (18, 26)
CLAIM_AMT = (26, 31)  # S9(7)V99 COMP-3


def unpack_comp3(buf: bytes) -> int:
    """Unpack signed packed decimal to integer cents (sign nibble C/F +, D -)."""
    nibbles = []
    for b in buf:
        nibbles.append(b >> 4)
        nibbles.append(b & 0x0F)
    sign = nibbles.pop()
    cents = int("".join(str(n) for n in nibbles))
    return -cents if sign == 0x0D else cents


def read_partition(path: Path, dt: str) -> list[tuple]:
    data = path.read_bytes()
    if len(data) % REC_LEN:
        raise ValueError(f"{path}: {len(data)} bytes is not a multiple of {REC_LEN}")
    rows = []
    for off in range(0, len(data), REC_LEN):
        rec = data[off:off + REC_LEN]
        cents = unpack_comp3(rec[CLAIM_AMT[0]:CLAIM_AMT[1]])
        rows.append((
            rec[CLAIM_ID[0]:CLAIM_ID[1]].decode("cp037"),
            rec[CLAIM_DATE[0]:CLAIM_DATE[1]].decode("cp037"),
            rec[CUST_ID[0]:CUST_ID[1]].decode("cp037"),
            Decimal(cents) / 100,
            dt,
        ))
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(prog="load_extract")
    p.add_argument("--landing", type=Path, required=True,
                   help="landing root containing dt=YYYY-MM-DD partitions")
    p.add_argument("--db", type=Path, required=True, help="duckdb warehouse path")
    a = p.parse_args(argv)

    rows = []
    for part in sorted(a.landing.glob("dt=*")):
        rows.extend(read_partition(part / "CLAIMS.DAT", part.name[3:]))

    con = duckdb.connect(str(a.db))
    try:
        con.execute("PRAGMA threads=1")
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.execute("""CREATE TEMP TABLE _stage (
            claim_id VARCHAR, claim_date VARCHAR, cust_id VARCHAR,
            claim_amt DECIMAL(9,2), dt VARCHAR)""")
        con.executemany("INSERT INTO _stage VALUES (?, ?, ?, ?, ?)", rows)
        con.execute("""
            CREATE OR REPLACE TABLE raw.claims_extract AS
            SELECT claim_id, claim_date, cust_id, claim_amt, dt
            FROM _stage
            ORDER BY claim_date, claim_id""")
    finally:
        con.close()
    print(f"loaded {len(rows)} claim rows into raw.claims_extract")


if __name__ == "__main__":
    sys.exit(main())
