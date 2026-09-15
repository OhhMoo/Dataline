"""excel_close theme hooks: close workbooks + raw load.

write_landing: one CLOSE_YYYYMM.xlsx per covered month, written by a minimal
stdlib xlsx writer (zipfile + string XML). Workspace bytes are fingerprinted,
so zip entries are ZIP_STORED with a fixed timestamp and fixed entry order —
never deflate (zlib output varies across hosts). One RNG per day keyed on
(seed, day_index); amounts are integer cents rendered %.2f and every cell is
an inline string — no binary floats, no sharedStrings, no numeric coercion.
load_raw: runs the workspace's own loader/import_close.py via subprocess, so
the script the agent reads is the code that actually loads.
"""
import random
import subprocess
import sys
import zipfile
from datetime import timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from world import scheduler, workflow
from world.scheduler import parse_vtime

GL_HEADER = ["line_id", "account", "post_date", "period", "amount"]
ADJ_HEADER = ["adj_id", "account", "period", "amount", "owner", "note"]
OWNERS = ["m.diaz", "j.chen", "a.patel", "s.okafor"]
NOTES = ["accrual true-up", "reclass per controller", "deferred rev catch-up",
         "prior-period correction", "write-off per memo"]

ZIP_TS = (1980, 1, 1, 0, 0, 0)


def _col(n: int) -> str:
    s = ""
    while True:
        s = chr(65 + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            return s


def _sheet_xml(rows: list[list[str]]) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           '<sheetData>']
    for ri, row in enumerate(rows, 1):
        out.append(f'<row r="{ri}">')
        for ci, val in enumerate(row):
            out.append(f'<c r="{_col(ci)}{ri}" t="inlineStr">'
                       f'<is><t>{escape(val)}</t></is></c>')
        out.append('</row>')
    out.append('</sheetData></worksheet>')
    return "".join(out)


def write_xlsx(path: Path, sheets: list[tuple[str, list]]) -> None:
    """Minimal stdlib .xlsx: inline strings only, fixed parts, fixed order."""
    ns = "http://schemas.openxmlformats.org"
    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          f'<Types xmlns="{ns}/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/'
          'vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/'
          'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
    ct += [f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType='
           '"application/vnd.openxmlformats-officedocument.spreadsheetml'
           f'.worksheet+xml"/>' for i in range(1, len(sheets) + 1)]
    ct.append('</Types>')
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 f'<Relationships xmlns="{ns}/package/2006/relationships">'
                 f'<Relationship Id="rId1" Type="{ns}/officeDocument/2006/'
                 'relationships/officeDocument" Target="xl/workbook.xml"/>'
                 '</Relationships>')
    wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          f'<workbook xmlns="{ns}/spreadsheetml/2006/main" '
          f'xmlns:r="{ns}/officeDocument/2006/relationships"><sheets>']
    wb += [f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>'
           for i, (name, _) in enumerate(sheets, 1)]
    wb.append('</sheets></workbook>')
    wb_rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               f'<Relationships xmlns="{ns}/package/2006/relationships">']
    wb_rels += [f'<Relationship Id="rId{i}" Type="{ns}/officeDocument/2006/'
                f'relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                for i in range(1, len(sheets) + 1)]
    wb_rels.append('</Relationships>')
    parts = [("[Content_Types].xml", "".join(ct)),
             ("_rels/.rels", root_rels),
             ("xl/workbook.xml", "".join(wb)),
             ("xl/_rels/workbook.xml.rels", "".join(wb_rels))]
    parts += [(f"xl/worksheets/sheet{i}.xml", _sheet_xml(rows))
              for i, (_, rows) in enumerate(sheets, 1)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, xml in parts:
            zi = zipfile.ZipInfo(name, date_time=ZIP_TS)
            # ZipInfo.__init__ sets create_system to 0 on win32 and 3 everywhere
            # else, so the identical workbook differed by one byte per entry
            # across hosts at identical total length - which is why this was
            # invisible to every length or row-count check. Pinned to 3 (Unix),
            # the value the episode container already produces.
            zi.create_system = 3
            zi.compress_type = zipfile.ZIP_STORED
            z.writestr(zi, xml.encode("utf-8"))


def _accounts(params: dict) -> list[str]:
    a = params["accounts"]
    return a if isinstance(a, list) else [f"{4000 + 100 * i}-GEN"
                                          for i in range(a)]


def line_rows(rng: random.Random, day, params: dict) -> list[list[str]]:
    lo_n, hi_n = params["lines_per_day"]
    lo_c, hi_c = params["amount_cents"]
    accounts = _accounts(params)
    return [[f"{rng.randrange(10**12):012d}", rng.choice(accounts),
             day.isoformat(), f"{day:%Y%m}",
             f"{rng.randint(lo_c, hi_c) / 100:.2f}"]
            for _ in range(rng.randint(lo_n, hi_n))]


def adj_rows(rng: random.Random, period: str, params: dict) -> list[list[str]]:
    lo_n, hi_n = params["adjustments_per_month"]
    lo_c, hi_c = params["amount_cents"]
    accounts = _accounts(params)
    return [[f"ADJ-{period}-{i + 1:02d}", rng.choice(accounts), period,
             f"{rng.choice((-1, 1)) * rng.randint(lo_c, hi_c) / 100:.2f}",
             rng.choice(OWNERS), rng.choice(NOTES)]
            for i in range(rng.randint(lo_n, hi_n))]


def months_upto(seed: dict, days: int) -> dict:
    """Period -> {lines, adj} for the first `days` covered days.

    A month's adjustments are drawn from the first covered day of that month,
    after that day's GL lines, keeping every draw on a per-day RNG. Because the
    landed days are always a prefix 0..days-1, this is a pure function of
    (seed, days) - which is what lets a workbook grow as virtual time passes
    and still be reproducible.
    """
    start = parse_vtime(seed["vtime_start"])
    months: dict[str, dict] = {}
    for i in range(days):
        day = (start + timedelta(days=i)).date()
        rng = random.Random(f"{seed['seed']}:{i}")
        lines = line_rows(rng, day, seed)
        bucket = months.setdefault(f"{day:%Y%m}", {"lines": [], "adj": None})
        bucket["lines"] += lines
        if bucket["adj"] is None:
            bucket["adj"] = adj_rows(rng, f"{day:%Y%m}", seed)
    return months


def _write_workbook(workspace: Path, period: str, bucket: dict) -> None:
    write_xlsx(workspace / "finance" / "workbooks" / f"CLOSE_{period}.xlsx",
               [("GL_LINES", [GL_HEADER] + bucket["lines"]),
                ("MANUAL_ADJ", [ADJ_HEADER] + bucket["adj"])])


def write_day(workspace: Path, seed: dict, day_index: int, day) -> dict:
    """Post one more day into its month's workbook.

    Excel is the integration layer here, so a "partition" is a month, not a
    file per day: the workbook for day's period is rewritten from every day
    landed so far. Same purity argument as months_upto.
    """
    period = f"{day:%Y%m}"
    bucket = months_upto(seed, day_index + 1)[period]
    _write_workbook(workspace, period, bucket)
    return {"workbook": f"CLOSE_{period}.xlsx", "lines": len(bucket["lines"])}


def write_landing(workspace: Path, seed: dict) -> None:
    """Write finance/workbooks/CLOSE_YYYYMM.xlsx for each covered month."""
    months = months_upto(seed, seed["days"])
    for period in sorted(months):
        _write_workbook(workspace, period, months[period])


def load_raw(workspace: Path, db_path: Path) -> None:
    r = subprocess.run(
        [sys.executable, str(workspace / "loader" / "import_close.py"),
         "--workbooks", str(workspace / "finance" / "workbooks"),
         "--db", str(db_path)],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"import_close failed:\n{r.stdout}\n{r.stderr}")


def write_workflow(root: Path, seed: dict, manifest: dict) -> None:
    """Seed var/workflow/{issues,chat,kb}.json per gateway/WORKFLOW_CONTRACT.md.

    The corpus is authored as data in this theme's workflow/ directory and
    dated against the world clock, which is read out of the scheduler state
    `build` has already written rather than re-derived from the seed - one
    definition of `vnow`, so a ticket cannot be filed after today.
    """
    workflow.seed(root, Path(__file__).resolve().parent, manifest["workflow"],
                  scheduler.load(root)["vnow"])
