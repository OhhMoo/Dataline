import yaml


def test_docs_and_cached_bi_expose_staleness(gateway):
    def call(tool, **args):
        result = gateway.call(tool, args)
        assert result["ok"], result
        return result["result"]

    assert call("docs_list")["entries"][0]["path"] == "docs/runbook.md"
    assert call("docs_get", path="docs/runbook.md")["lines"][0]["line"] == 1
    assert not gateway.call("docs_get", {"path": "README.md"})["ok"]
    path = gateway.config.dashboards / "item_totals.yml"
    spec = yaml.safe_load(path.read_text())
    spec["owner"] = "finance"
    spec["metrics"] = [
        {
            "name": "amount",
            "expression": "sum(amount)",
            "owner": "finance",
            "doc": "docs/runbook.md",
        }
    ]
    spec["panels"][0]["watermark_sql"] = "select max(id) as id from warehouse.items"
    path.write_text(yaml.safe_dump(spec))
    assert call("bi_get", dashboard="item_totals")["metrics"][0]["doc"] == "docs/runbook.md"
    before = call("bi_refresh", dashboard="item_totals")
    call("sql_execute", sql="update warehouse.items set amount=30 where id=1")
    assert call("bi_cached", dashboard="item_totals") == before
    after = call("bi_refresh", dashboard="item_totals")
    assert after["panels"] != before["panels"]
    assert after["last_refreshed_at"] == gateway.clock()
    assert len(after["refresh_events"]) == 2
    assert after["refresh_events"][0]["source_watermarks"]["summary"]["rows"] == [[2]]


def test_bi_filter_and_failed_refresh_preserve_cache(gateway):
    path = gateway.config.dashboards / "item_totals.yml"
    spec = yaml.safe_load(path.read_text())
    spec["panels"] = [{"name": "one", "sql": "select * from warehouse.items", "row_filter": "id=1"}]
    path.write_text(yaml.safe_dump(spec))
    result = gateway.call("bi_refresh", {"dashboard": "item_totals"})
    assert result["result"]["panels"]["one"]["rows"][0][0] == 1
    spec["panels"][0]["sql"] = "select * from missing_table"
    path.write_text(yaml.safe_dump(spec))
    assert not gateway.call("bi_refresh", {"dashboard": "item_totals"})["ok"]
    assert gateway.call("bi_cached", {"dashboard": "item_totals"})["result"] == result["result"]
