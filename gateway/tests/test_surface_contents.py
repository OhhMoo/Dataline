"""Dashboard content, audit attribution, and incomplete-document diagnostics."""

import json

import pytest
import yaml

from alkera_gateway.audit import read_rows
from alkera_gateway.records import digest

SALES = {
    "version": 1,
    "dashboard": "sales_daily",
    "title": "Sales, daily",
    "owner": "revops",
    "metrics": [
        {
            "name": "gross_amount",
            "expression": "sum(amount)",
            "owner": "revops",
            "doc": "docs/runbook.md",
        }
    ],
    "panels": [
        {
            "name": "total",
            "sql": "select sum(amount) as amount from warehouse.items",
            "title": "Gross",
            "metric": "gross_amount",
            "row_filter": None,
            "watermark_sql": "select max(id) as id from warehouse.items",
        }
    ],
}


def assert_ok(result):
    assert result["ok"], result["error"]
    return result["result"]


def seed(gateway, spec):
    path = gateway.config.dashboards / f"{spec['dashboard']}.yml"
    path.write_text(yaml.safe_dump(spec, sort_keys=True), encoding="utf-8")
    return path


def test_bi_list_returns_whole_definitions_not_stubs(gateway):
    """Every field an archetype reasons over has to survive the listing.

    Fifteen archetypes need "a number on a dashboard, the definition behind it, and
    when it last refreshed" (T1). The definition half is this call, so it is asserted
    field by field - owner, metric expression, the panel's literal SQL, the watermark
    query - not merely counted.
    """
    seed(gateway, SALES)

    listing = assert_ok(gateway.call("bi_list", {}))
    names = [item["dashboard"] for item in listing["dashboards"]]
    # Sorted by definition filename, and the fixture's own dashboard is still there.
    assert names == ["item_totals", "sales_daily"]

    sales = next(item for item in listing["dashboards"] if item["dashboard"] == "sales_daily")
    assert sales["title"] == "Sales, daily"
    assert sales["owner"] == "revops"
    assert sales["version"] == 1
    assert sales["metrics"] == [
        {
            "name": "gross_amount",
            "expression": "sum(amount)",
            "owner": "revops",
            "doc": "docs/runbook.md",
        }
    ]
    panel = sales["panels"][0]
    assert panel["sql"] == "select sum(amount) as amount from warehouse.items"
    assert panel["metric"] == "gross_amount"
    assert panel["watermark_sql"] == "select max(id) as id from warehouse.items"

    # The listing is the same object bi_get serves, so an agent can enumerate then
    # drill in without the two views disagreeing.
    assert sales == assert_ok(gateway.call("bi_get", {"dashboard": "sales_daily"}))

    # The SQL in the listing is real SQL: it runs, and it returns the fixture's data.
    computed = assert_ok(gateway.call("sql_query", {"sql": panel["sql"]}))
    assert computed["rows"] == [[{"type": "decimal", "value": "30.75"}]]


def test_bi_list_reads_live_definitions_and_logs_a_read(gateway):
    """bi_list must reflect a bi_update immediately - no cached index - and must land
    a well-formed 9.2.2 row naming every dashboard it read."""
    before = assert_ok(gateway.call("bi_list", {}))
    assert [item["dashboard"] for item in before["dashboards"]] == ["item_totals"]

    assert_ok(gateway.call("bi_update", {"dashboard": "sales_daily", "definition": SALES}))
    after = assert_ok(gateway.call("bi_list", {}))
    assert [item["dashboard"] for item in after["dashboards"]] == ["item_totals", "sales_daily"]
    assert after["dashboards"][1]["owner"] == "revops"

    rows = read_rows(gateway.audit.actions)
    assert [row["seq"] for row in rows] == [1, 2, 3]
    assert [row["tool"] for row in rows] == ["bi_list", "bi_update", "bi_list"]
    assert [row["op_class"] for row in rows] == ["read", "write", "read"]
    assert [row["exit"] for row in rows] == ["ok", "ok", "ok"]

    first, update, second = rows
    assert first["targets"] == first["read_targets"] == ["dashboard:item_totals"]
    assert second["targets"] == ["dashboard:item_totals", "dashboard:sales_daily"]
    assert update["targets"] == ["dashboard:sales_daily"]
    assert update["flags"] == []  # a new dashboard is not an overwrite
    # op_class is a log field, not a response field (agent.md 9.2.2).
    assert "op_class" not in second["result"]
    # bi_list takes no arguments, so its hash must still be the stable hash of {}.
    assert first["args"] == {}
    assert first["args_hash"] == second["args_hash"] == digest({})

    # A second bi_update over the same id is an overwrite and says so.
    assert_ok(gateway.call("bi_update", {"dashboard": "sales_daily", "definition": SALES}))
    assert read_rows(gateway.audit.actions)[3]["flags"] == ["overwrite"]


def test_bi_list_fails_loudly_on_an_unreadable_definition(gateway):
    """A dashboard directory holding something that is not a dashboard must not produce
    a shorter listing that looks complete. Failing is the correct answer here; silently
    listing the readable ones would hide a broken world from the agent and the grader."""
    (gateway.config.dashboards / "notes.yml").write_text("hello: world\n", encoding="utf-8")
    result = gateway.call("bi_list", {})
    assert result["ok"] is False
    assert result["result"] is None
    row = read_rows(gateway.audit.actions)[0]
    assert row["exit"] == "error"
    assert row["tool"] == "bi_list"


def test_docs_search_does_not_silently_drop_unreadable_documents(gateway):
    """B1-FINISH: fixed. The xfail(strict) marker B3 left here has been removed.

    `Workspace.search` now names every document it could not read, and `Search`
    takes an `encoding` symmetric with fs_read, so the second half of the
    assertion below - that the skipped document is actually reachable - holds too.
    """
    docs = gateway.config.workspace / "docs"
    # Both of these contain the search term.
    docs.joinpath("vendor.md").write_bytes("Caf\xe9 load is idempotent\n".encode("cp1252"))
    docs.joinpath("huge.md").write_bytes(
        b"x" * (gateway.config.max_payload_bytes + 10) + b"\nidempotent\n"
    )

    result = assert_ok(gateway.call("docs_search", {"query": "idempotent"}))
    evidence = json.dumps(result)
    # Either searched, or named as unsearchable. Silence about them is the defect.
    assert "docs/vendor.md" in evidence
    assert "docs/huge.md" in evidence

    # And the reason is diagnostic, not a shrug: one is the wrong codec, one is
    # too large. limit_reached=false is now only a claim about the limit.
    skipped = {entry["path"]: entry for entry in result["skipped"]}
    assert skipped["docs/vendor.md"]["code"] == "decode_error"
    assert "offset" in skipped["docs/vendor.md"]["reason"]
    assert skipped["docs/huge.md"]["code"] == "file_too_large"
    assert result["limit_reached"] is False

    # Naming it is only half a fix if the agent still cannot get at it.
    recovered = assert_ok(
        gateway.call("docs_search", {"query": "idempotent", "encoding": "cp1252"})
    )
    hits = {match["path"] for match in recovered["matches"]}
    assert "docs/vendor.md" in hits
    assert [entry["path"] for entry in recovered["skipped"]] == ["docs/huge.md"]
    # A codec that does not exist is the agent's error, not the world's, so it
    # fails the whole call plainly rather than skipping every document in silence.
    assert (
        gateway.call("docs_search", {"query": "x", "encoding": "cp9999"})["error"]["code"]
        == "unknown_encoding"
    )


@pytest.mark.parametrize("content", ["hello: world\n", "panels: [\n"])
@pytest.mark.parametrize("tool", ["bi_list", "bi_get", "bi_render"])
def test_bad_dashboard_names_the_world_defect(gateway, content, tool):
    (gateway.config.dashboards / "notes.yml").write_text(content, encoding="utf-8")
    args = {} if tool == "bi_list" else {"dashboard": "notes"}
    error = gateway.call(tool, args)["error"]
    assert error["code"] == "invalid_dashboard"
    assert "notes" in error["message"]


def test_invalid_dashboard_update_is_still_an_argument_error(gateway):
    result = gateway.call("bi_update", {"dashboard": "notes", "definition": {"hello": "world"}})
    assert result["error"]["code"] == "invalid_arguments"
    assert not (gateway.config.dashboards / "notes.yml").exists()
