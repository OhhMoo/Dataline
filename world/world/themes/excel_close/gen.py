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


def write_landing(workspace: Path, seed: dict) -> None:
    """Write finance/workbooks/CLOSE_YYYYMM.xlsx for each covered month.

    A month's adjustments are drawn from the first covered day of that month,
    after that day's GL lines, keeping every draw on a per-day RNG.
    """
    start = parse_vtime(seed["vtime_start"])
    months: dict[str, dict] = {}
    for i in range(seed["days"]):
        day = (start + timedelta(days=i)).date()
        rng = random.Random(f"{seed['seed']}:{i}")
        lines = line_rows(rng, day, seed)
        bucket = months.setdefault(f"{day:%Y%m}", {"lines": [], "adj": None})
        bucket["lines"] += lines
        if bucket["adj"] is None:
            bucket["adj"] = adj_rows(rng, f"{day:%Y%m}", seed)
    for period in sorted(months):
        wb = workspace / "finance" / "workbooks" / f"CLOSE_{period}.xlsx"
        write_xlsx(wb, [("GL_LINES", [GL_HEADER] + months[period]["lines"]),
                        ("MANUAL_ADJ", [ADJ_HEADER] + months[period]["adj"])])


def load_raw(workspace: Path, db_path: Path) -> None:
    r = subprocess.run(
        [sys.executable, str(workspace / "loader" / "import_close.py"),
         "--workbooks", str(workspace / "finance" / "workbooks"),
         "--db", str(db_path)],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"import_close failed:\n{r.stdout}\n{r.stderr}")
