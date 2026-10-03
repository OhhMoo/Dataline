"""The semantic core: one model, three BI skins (docs/environments/BI_BUILD_PLAN.md).

Power BI's semantic model (TMDL), Tableau's data sources and Sigma's workbooks are
the same thing underneath: tables over warehouse relations, relationships between
them, measures evaluated in a filter context, and visuals that ask for measures by
some dimensions. This package is that thing, once:

    model.py      the model (tables, columns, relationships, measures, visuals,
                  reports) - THE CONTRACT every front-end and the engine share
    expr.py       the expression tree every vendor language lowers into, and the
                  filter modifiers CALCULATE / FIXED / grouping lower into
    errors.py     Unsupported (loud, named) and ModelError
    examples.py   the Commerce model over the commerce_analytics star schema, and
                  a tiny deterministic DuckDB fixture of that schema for tests
    compile.py    expression + filter context -> SQL            (engine)
    render.py     visual -> canonical {columns, rows}            (engine)
    formats/      one module per front-end:
                    yaml_model.py   dbt semantic models + metrics (BI-as-code)
                    powerbi/        PBIP: TMDL semantic model + PBIR report; DAX
                    tableau/        .tds / .twb XML; calculated fields
                    sigma/          workbook / data-model JSON; formulas

Every front-end module exposes the same four functions:

    read_project(path: Path, *, warehouse: Path | None = None) -> tuple[Model, tuple[Report, ...]]
    write_project(model: Model, reports: tuple[Report, ...], dest: Path) -> list[Path]
    parse_expression(text: str, model: Model, *, table: str | None = None) -> Expr
    format_expression(expr: Expr, model: Model, *, table: str | None = None) -> str

`warehouse` lets a front-end whose files carry no column types (Sigma) read them
from the warehouse; `table` is the table a row-level expression is written in
(a calculated column's home), for languages whose references are relative to it.
Front-ends that need neither accept and ignore them.

`write_project` is deterministic (same inputs, same bytes: sorted keys, ids derived
from names, no timestamps), because its output is fingerprinted workspace content.
`read_project(write_project(m, r, d))` must give back an equal model and reports.

Two rules hold everywhere:

1. LOUD UNSUPPORTED, NEVER SILENT WRONG. Anything a front-end cannot lower exactly,
   or the engine cannot evaluate exactly, raises `errors.Unsupported` naming the
   feature in the vendor's own words. No approximations, ever.
2. THE GRADER RECOMPUTES. Graders call `render` on the agent's final files in the
   grading zone. Caches, service responses and screenshots are never outcome.

world/ is the lowest layer: nothing here imports alkera_gateway or tasks/.
"""
