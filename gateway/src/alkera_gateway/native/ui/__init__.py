"""Browser UI clones of the BI vendors, served over the fake services' PUBLISHED
state (docs/environments/UI_CLONE_PLAN.md).

    html.py      vendor-neutral, server-rendered HTML and inline SVG (no JS)
    tableau.py   Tableau Server's pages: `mount(app)` onto services.tableau.app,
                 or `standalone()` for tests

A UI module is a client of its service module, never a second store: it uses
the service's Store, lock, publish path and audit row, so a click and a REST
call land in the same published state and the same action log.
"""
