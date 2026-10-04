"""The three BI vendor services over a BUILT world - no fixture project, no Docker.

Every vendor service's own suite seeds its published state from a project the
test wrote by hand over the tiny Commerce fixture. This one builds the real
thing - a commerce_analytics world whose seed profile plugs Power BI, Tableau
and Sigma, so world.build writes each vendor's project into the workspace and
commits it (world/world/bi_projects.py) - and drives each service in-process,
configured the way its own suite configures it. What is proved:

1. PROVISIONING READS THE WORLD. Each service's initial published state is the
   project the build checked in: Power BI's dataset and report from the
   workspace's first git commit, Tableau's data source and workbook, Sigma's
   data model and workbook.
2. THE NUMBERS ARE THE ENGINE'S ON THE FULL-SIZE WAREHOUSE. Power BI
   executeQueries for one visual, Tableau's view data for every sheet, Sigma's
   export of every element equal `world.semantic.render.render_report` of the
   same vendor's files in the workspace on the built warehouse - the values the
   graders recompute.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import sys
import xml.etree.ElementTree as ET
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
# Before the services: tableau.py imports world.semantic at module level.
if str(REPO / "world") not in sys.path:
    sys.path.insert(0, str(REPO / "world"))

from world import build, fingerprint  # noqa: E402
from world.semantic import render  # noqa: E402
from world.semantic.formats import powerbi as pbi_format  # noqa: E402
from world.semantic.formats import sigma as sigma_format  # noqa: E402
from world.semantic.formats import tableau as tableau_format  # noqa: E402
from world.semantic.formats.powerbi import daxquery  # noqa: E402

from alkera_gateway import GatewayConfig  # noqa: E402
from alkera_gateway.native import context as native_context  # noqa: E402
from alkera_gateway.native.services import powerbi, sigma, tableau  # noqa: E402

SEED = REPO / "world" / "world" / "themes" / "commerce_analytics" / "seed.example.yaml"
PROFILE = {"name": "da/laptop",
           "slots": ["files", "notebook", "warehouse", "transform", "bi", "human"],
           "plugs": {"bi": ["powerbi", "sigma", "tableau"]}}
TENANT = "7f3c2a10-5b8e-4d6f-9a1c-2e4b6d8f0a12"
CLIENT = "3b9d6e2f-0c4a-4f1e-8b7d-5a6c9e1f2d34"
V = "3.25"
NS = {"t": "http://tableau.com/api"}


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Path:
    tmp = tmp_path_factory.mktemp("bi_world")
    seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    path = tmp / "seed.yaml"
    path.write_text(yaml.safe_dump({**seed, "profile": PROFILE}, sort_keys=True),
                    encoding="utf-8")
    return build.build(path, tmp / "world")


@pytest.fixture(scope="module")
def fp(world) -> str:
    return fingerprint.semantic_hash(world)


def settings(world: Path, audit: Path, fp: str) -> GatewayConfig:
    return GatewayConfig(
        workspace=world / "workspace",
        warehouse=world / "var" / "warehouse" / "warehouse.duckdb",
        scheduler=world / "var" / "sched" / "state.json",
        dashboards=world / "var" / "bi" / "dashboards",
        observability=world / "var" / "obs" / "definitions.yaml",
        workflow_dir=world / "var" / "workflow",
        audit_dir=audit,
        profiles_dir=world / "build",
        episode_id=audit.name,
        initial_world_fp=fp)


@pytest.fixture
def config(world, fp, tmp_path, monkeypatch) -> GatewayConfig:
    """A fresh audit dir (and so a fresh <bi_state_dir>) per test: every service
    provisions from the world on its first request."""
    cfg = settings(world, tmp_path / "audit", fp)
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(tmp_path / "native-actions.jsonl"))
    monkeypatch.setenv("ALKERA_SCHEDULER", str(cfg.scheduler))
    return cfg


def rendered(world: Path, vendor: str, front_end) -> tuple:
    """The vendor's model and report as the WORKSPACE files say, and the engine's
    values for them on the built warehouse."""
    warehouse = world / "var" / "warehouse" / "warehouse.duckdb"
    model, (report,) = front_end.read_project(world / "workspace" / "bi" / vendor,
                                              warehouse=warehouse)
    return model, report, render.render_report(model, report, warehouse)


# ======================================================================
# Power BI: the dataset and report from the first commit; executeQueries
# ======================================================================
def test_power_bi_publishes_the_committed_project_and_answers_its_numbers(world, config):
    native_context.set_context(powerbi.app, native_context.Context(config))
    try:
        client = TestClient(powerbi.app, base_url="http://powerbi.internal:8095")
        token = client.post(f"/{TENANT}/oauth2/v2.0/token", data={
            "grant_type": "client_credentials", "client_id": CLIENT,
            "client_secret": native_context.DEV_TOKEN,
            "scope": "https://analysis.windows.net/powerbi/api/.default"})
        assert token.status_code == 200, token.text
        auth = {"Authorization": f"Bearer {token.json()['access_token']}"}
        group = client.get("/v1.0/myorg/groups", headers=auth).json()["value"][0]["id"]
        datasets = client.get(f"/v1.0/myorg/groups/{group}/datasets", headers=auth).json()
        assert [d["name"] for d in datasets["value"]] == ["Commerce"]
        reports = client.get(f"/v1.0/myorg/groups/{group}/reports", headers=auth).json()
        assert [r["name"] for r in reports["value"]] == ["Executive Overview"]

        model, report, values = rendered(world, "powerbi", pbi_format)
        text = ('EVALUATE SUMMARIZECOLUMNS ( Customer[segment], "Net Revenue", '
                '[Net Revenue], "Segment Share", [Segment Share] ) '
                'ORDER BY [Net Revenue] DESC')
        response = client.post(
            f"/v1.0/myorg/groups/{group}/datasets/{datasets['value'][0]['id']}"
            f"/executeQueries", headers=auth, json={"queries": [{"query": text}]})
        assert response.status_code == 200, response.text
        query = daxquery.parse_query(text, model)
        warehouse = world / "var" / "warehouse" / "warehouse.duckdb"
        assert response.json() == daxquery.execute_queries_result(
            query, render.render_visual(query.model, query.visual, warehouse))
        # ... and it is the report's own visual, as the grader renders it
        got = [[r["Customer[segment]"], r["[Net Revenue]"], r["[Segment Share]"]]
               for r in response.json()["results"][0]["tables"][0]["rows"]]
        want = values["Overview"]["Revenue by Segment"]
        assert want["columns"] == ["segment", "Net Revenue", "Segment Share"]
        assert got == [[s, float(net), float(share)] for s, net, share in want["rows"]]
        assert len(got) == 3
    finally:
        del powerbi.app.state.alkera_context


# ======================================================================
# Tableau: the data source and workbook from the workspace; view data
# ======================================================================
def _tableau_cell(value, fmt: str | None, measure: bool) -> str:
    """What a view's CSV shows for a rendered value - written out here, not
    borrowed from the service: Excel-style formats, half away from zero."""
    if value is None:
        return ""
    if not measure:
        return str(value)
    if fmt == "$#,0.00":
        return f"${Decimal(value).quantize(Decimal('0.01'), ROUND_HALF_UP):,.2f}"
    if fmt == "0.0%":
        return f"{(Decimal(value) * 100).quantize(Decimal('0.1'), ROUND_HALF_UP):.1f}%"
    assert fmt is None, fmt
    return f"{value:,}" if isinstance(value, int) else str(value)


def test_tableau_publishes_the_workspace_project_and_serves_its_numbers(world, config):
    native_context.set_context(tableau.app, native_context.Context(config))
    try:
        client = TestClient(tableau.app, base_url="http://tableau.internal:8096")
        signin = client.post(f"/api/{V}/auth/signin", content=(
            f'<tsRequest><credentials personalAccessTokenName="ci" '
            f'personalAccessTokenSecret="{native_context.DEV_TOKEN}">'
            f'<site contentUrl="" /></credentials></tsRequest>'))
        assert signin.status_code == 200, signin.text
        creds = ET.fromstring(signin.content).find("t:credentials", NS)
        headers = {"X-Tableau-Auth": creds.get("token")}
        site = creds.find("t:site", NS).get("id")

        def get(path):
            r = client.get(f"/api/{V}/sites/{site}/{path}", headers=headers)
            assert r.status_code == 200, r.text
            return r

        names = [w.get("name") for w in ET.fromstring(get("workbooks").content).iter(
            f"{{{NS['t']}}}workbook")]
        assert names == ["Executive Overview"]
        sources = [d.get("name") for d in ET.fromstring(get("datasources").content).iter(
            f"{{{NS['t']}}}datasource")]
        assert sources == ["Commerce"]

        model, report, values = rendered(world, "tableau", tableau_format)
        formats = {m.name: m.format_string for m in model.measures}
        checked = 0
        for page in report.pages:
            for visual in page.visuals:
                url = f"ExecutiveOverview/sheets/{visual.name.replace(' ', '')}"
                view = ET.fromstring(get(f"views?filter=contentUrl:eq:{url}").content).find(
                    "t:views/t:view", NS)
                body = get(f"views/{view.get('id')}/data")
                assert body.headers["content-type"].startswith("text/csv")
                got = list(csv.reader(io.StringIO(body.text)))
                want = values[page.name][visual.name]
                k = len(visual.group_by)
                fmts = [None] * k + [formats.get(getattr(e, "name", None))
                                     for _, e in visual.values]
                assert got == [want["columns"]] + [
                    [_tableau_cell(v, fmts[i], i >= k) for i, v in enumerate(row)]
                    for row in want["rows"]], visual.name
                checked += 1
        assert checked == 5
    finally:
        del tableau.app.state.alkera_context


# ======================================================================
# Sigma: the data model and workbook from the workspace; export + download
# ======================================================================
def test_sigma_publishes_the_workspace_project_and_exports_its_numbers(world, config):
    native_context.set_context(sigma.app, native_context.Context(config))
    try:
        client = TestClient(sigma.app, base_url="http://sigma.internal")
        basic = base64.b64encode(f"ci-client:{native_context.DEV_TOKEN}".encode()).decode()
        token = client.post("/v2/auth/token", data={"grant_type": "client_credentials"},
                            headers={"Authorization": f"Basic {basic}"})
        assert token.status_code == 200, token.text
        auth = {"Authorization": f"Bearer {token.json()['access_token']}"}
        assert client.get("/health").json()["items"] == 2      # 1 data model, 1 workbook
        listed = client.get("/v2/workbooks", headers=auth).json()["entries"]
        assert [w["name"] for w in listed] == ["Executive Overview"]

        repo = world / "workspace" / "bi" / "sigma"
        doc = json.loads((repo / "workbooks" / "executive-overview.json").read_text("utf-8"))
        assert listed[0]["workbookId"] == doc["workbookId"]
        model, report, values = rendered(world, "sigma", sigma_format)
        visuals = {v.name: (p.name, v) for p in report.pages for v in p.visuals}
        elements = doc["contents"]["elements"]
        assert sorted(el["name"] for el in elements) == sorted(visuals)
        for el in elements:
            job = client.post(f"/v2/workbooks/{doc['workbookId']}/export", headers=auth,
                              json={"elementId": el["id"], "format": {"type": "csv"}})
            assert job.status_code == 200, job.text
            download = client.get(f"/v2/query/{job.json()['queryId']}/download",
                                  headers=auth)
            assert download.status_code == 200, download.text
            page, _ = visuals[el["name"]]
            want = values[page][el["name"]]
            assert list(csv.reader(io.StringIO(download.text))) == [want["columns"]] + [
                ["" if v is None else str(v) for v in row] for row in want["rows"]], \
                el["name"]
    finally:
        del sigma.app.state.alkera_context
