"""Deterministic CSV landing and raw loading used by the new enterprise themes.

Each theme owns its schema and row generator. This small adapter only implements
the world.build / world.scheduler hook protocol, without hidden wall-clock state.
"""

import csv
from datetime import timedelta
from pathlib import Path

import duckdb

from world import scheduler, workflow


class ThemeRuntime:
    def __init__(self, theme_file, tables, rows_for_day):
        self.theme_dir = Path(theme_file).resolve().parent
        self.tables = tables
        self.rows_for_day = rows_for_day

    def write_day(self, workspace, seed, day_index, day):
        result = self.rows_for_day(seed, day_index, day)
        total = 0
        for table, columns in self.tables.items():
            rows = result[table]
            folder = Path(workspace) / "data" / "landing" / table / f"dt={day}"
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "events.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(columns), lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            total += len(rows)
        return {"partition": f"dt={day}", "rows": total}

    def write_landing(self, workspace, seed):
        start = scheduler.parse_vtime(seed["vtime_start"])
        for index in range(seed["days"]):
            self.write_day(workspace, seed, index, (start + timedelta(days=index)).date())

    def load_raw(self, workspace, db_path):
        con = duckdb.connect(str(db_path))
        try:
            con.execute("PRAGMA threads=1")
            con.execute("CREATE SCHEMA IF NOT EXISTS raw")
            for table, columns in self.tables.items():
                path = (Path(workspace) / "data" / "landing" / table).as_posix()
                # Only the CSV columns are declared here. Hive partitioning adds
                # dt from the directory, independently of column ordering.
                types = "{" + ", ".join(f"'{name}': '{kind}'" for name, kind in columns.items()) + "}"
                escaped = (path + "/dt=*/*.csv").replace("'", "''")
                con.execute(
                    f"CREATE OR REPLACE TABLE raw.{table} AS "
                    f"SELECT * FROM read_csv('{escaped}', hive_partitioning=true, "
                    f"columns={types}) ORDER BY ALL"
                )
        finally:
            con.close()

    def write_workflow(self, root, seed, manifest):
        workflow.seed(root, self.theme_dir, manifest["workflow"],
                      scheduler.load(root)["vnow"])
