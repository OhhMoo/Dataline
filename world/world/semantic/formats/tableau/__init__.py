"""Tableau front-end: .tds / .twb XML and calculated fields (see each module).

    project.py   read_project / write_project; the on-disk convention
                 (<workspace>/bi/tableau/{datasources,workbooks}/)
    tds.py       <datasource> <-> Model: connections, logical tables, physical
                 joins (lowered to SQL), relationships, calculated fields
    twb.py       <workbook> <-> Report: worksheets -> Visuals, dashboards -> Pages
    calc.py      the calculation language <-> the expression tree
    xmlio.py     Tableau's own XML house style; `_.fcp.` feature prefixes
    examples.py  the Commerce model as a Tableau author writes it, and why the
                 Power BI-shaped examples.commerce_model() cannot round-trip

Sources: github.com/tableau/document-api-python (MIT), github.com/tableau/
server-client-python (MIT), github.com/tableau/tabcmd (MIT),
github.com/tableau/tableau-document-schemas (Apache-2.0), the Tableau REST API
reference and calculation function reference on help.tableau.com.
"""
from .project import format_expression, parse_expression, read_project, write_project

__all__ = ["read_project", "write_project", "parse_expression", "format_expression"]
