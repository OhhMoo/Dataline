"""Power BI: a PBIP project (TMDL semantic model + PBIR report) and DAX.

The four front-end functions every format exposes (world.semantic docstring):

    read_project(path)                      .pbip / project folder -> (Model, reports)
    write_project(model, reports, dest)     -> written paths, deterministic bytes
    parse_expression(text, model)           DAX -> expr tree (or Unsupported/ModelError)
    format_expression(expr, model)          expr tree -> idiomatic DAX

plus the DAX query subset Power BI's executeQueries REST call takes
(`daxquery.parse_query`, `daxquery.execute_queries_result`).

Modules: project.py (the PBIP layout), tmdl.py (semantic model), pbir.py
(report), mquery.py (Power Query partition sources), dax.py (measures),
daxquery.py (EVALUATE ...), ids.py (deterministic GUIDs and 20-hex names).
Everything outside the subset raises errors.Unsupported(feature, "Power BI"),
the feature named the way Power BI names it.
"""
from __future__ import annotations

from .dax import format_expression, parse_expression
from .project import read_project, write_project

__all__ = ["format_expression", "parse_expression", "read_project", "write_project"]
