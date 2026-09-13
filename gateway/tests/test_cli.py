from alkera_gateway.cli import parse_pairs


def test_parse_pairs_supports_shell_friendly_arguments():
    assert parse_pairs(["sql=select 1", "limit=25", "full_refresh=false"]) == {
        "sql": "select 1",
        "limit": 25,
        "full_refresh": False,
    }
