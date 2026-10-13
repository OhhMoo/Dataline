"""Browser UI clones of the BI vendors, served over the fake services' PUBLISHED
state (docs/environments/UI_CLONE_PLAN.md).

    html.py      vendor-neutral, server-rendered HTML and inline SVG (no JS)
    tableau.py   Tableau Server's pages: services.tableau mounts them into its
                 own app at import (`mount(app)`); `standalone()` for tests
    sigma.py     Sigma's workbook, element and data-model pages under /alkera:
                 services.sigma mounts them at the end of its module and keeps
                 its `/{path:path}` fallback the last route

A UI module is a client of its service module, never a second store: it uses
the service's Store, lock, publish path and audit row, so a click and a REST
call land in the same published state and the same action log.
"""
