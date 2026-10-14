"""browser: Playwright for Python driving headless Chromium through the BI vendors' web
UIs on the analyst's box (da/laptop) - the Tableau Server pages at tableau.internal:8096
and the Sigma app at sigma.internal:8097, each mounted in its service's own app.

Plan decision D5: an agent drives those UIs with a real Playwright script over the
Bash surface it already has, and the audit stays server-side - one `tableau.ui_*` /
`sigma.ui_*` row per page the browser asks for, written before the page is served.
The da/ds images carry Playwright and the Chromium headless shell its version pins
(gateway/docker/TOOLBELT.md, "Roles"); a de box has neither.

Each command writes a Playwright script and runs it with the box's own `python3`, so
the PATH shim audits the process (`shim.python`). The script opens a page, is sent to
the vendor's sign-in form, signs in, and reads Net Revenue out of the page's table
(`<table class="summary">`, the stable hook of native/ui/html.py and native/ui/sigma.py),
whose cells are the REST export's cell for cell.

The browsers' passwords are service constants (`services.tableau.UI_PASSWORD`,
`services.sigma.UI_PASSWORD`) and by design in no box env or discovery file: these
commands are oracles, so they state them, and each `instruction` hands the sign-in to
the agent the way a task's brief does.
"""
from tool_tour import Probe

from tool_tour_probes import DA_LAPTOP_BI, NET_REVENUE_SQL

PROFILE, PLUGS = DA_LAPTOP_BI
_DIR = "/tmp/tour_browser"

PROBES = [
    Probe(
        id="browser.playwright.tableau_view_net_revenue",
        profile=PROFILE,
        plugs=PLUGS,
        command=f"""cd /workspace
set -euo pipefail
mkdir -p {_DIR}
cat > {_DIR}/tableau_kpis.py <<'PY'
import json, os
from playwright.sync_api import sync_playwright

server = os.environ["TABLEAU_SERVER_URL"].rstrip("/")
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    # the view's own URL: signed out, the server sends the browser to Sign In and back
    page.goto(server + "/views/ExecutiveOverview/KPIs")
    page.fill("#signin input[name=username]", "analyst")
    page.fill("#signin input[name=password]", "alkera-local-dev-ui")
    page.click("#signin button[type=submit]")
    table = page.locator("table.summary").first
    table.wait_for()
    head = table.locator("thead th").all_inner_texts()
    cells = table.locator("tbody tr").first.locator("td").all_inner_texts()
    browser.close()
net = dict(zip(head, cells))["Net Revenue"]
print(json.dumps({{"value": net.replace("$", "").replace(",", "")}}))
PY
python3 {_DIR}/tableau_kpis.py
""",
        expect_tools=("tableau.ui_signin", "tableau.ui_view", "shim.python"),
        truth_sql=NET_REVENUE_SQL,
        instruction=(
            "With Playwright in this box's Python, drive a headless browser: sign in to "
            "the Tableau Server web UI at http://tableau.internal:8096 as any user with "
            "the password alkera-local-dev-ui, open the KPIs view of the Executive "
            "Overview workbook, and report the Net Revenue its table shows, as a number."),
    ),
    Probe(
        id="browser.playwright.sigma_element_net_revenue",
        profile=PROFILE,
        plugs=PLUGS,
        command=f"""cd /workspace
set -euo pipefail
mkdir -p {_DIR}
cat > {_DIR}/sigma_kpis.py <<'PY'
import json, os
from playwright.sync_api import sync_playwright

app = os.environ["SIGMA_BASE_URL"].rstrip("/")
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    # Home: signed out, Sigma sends the browser to its sign-in form and back
    page.goto(app + "/alkera")
    page.fill("#login input[name=email]", "analyst@example.com")
    page.fill("#login input[name=password]", "alkera-local-ui")
    page.click("#login button[type=submit]")
    page.locator("table.documents[data-kind=workbook]").get_by_role(
        "link", name="Executive Overview", exact=True).click()
    kpis = page.locator("section[data-element]").filter(
        has=page.get_by_role("heading", name="KPIs", exact=True))
    table = kpis.locator("table.summary")
    table.wait_for()
    head = table.locator("thead th").all_inner_texts()
    cells = table.locator("tbody tr").first.locator("td").all_inner_texts()
    browser.close()
print(json.dumps({{"value": dict(zip(head, cells))["Net Revenue"]}}))
PY
python3 {_DIR}/sigma_kpis.py
""",
        expect_tools=("sigma.ui_login", "sigma.ui_workbook", "shim.python"),
        truth_sql=NET_REVENUE_SQL,
        instruction=(
            "With Playwright in this box's Python, drive a headless browser: sign in to "
            "the Sigma web app at http://sigma.internal:8097/alkera with any email and "
            "the password alkera-local-ui, open the Executive Overview workbook, and "
            "report the Net Revenue its KPIs element's table shows, as a number."),
    ),
]
