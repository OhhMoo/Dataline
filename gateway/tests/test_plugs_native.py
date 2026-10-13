"""Native mode honours an environment profile (alkera_gateway.plugs)."""
from __future__ import annotations

import pytest

from alkera_gateway.native import serve, services
from alkera_gateway.plugs import ProfileError


def test_load_without_names_covers_every_service():
    loaded, missing = services.load()
    assert set(loaded) | set(missing) == set(services.NAMES)


def test_load_with_names_covers_exactly_those():
    loaded, missing = services.load(("jira",))
    assert set(loaded) | set(missing) == {"jira"}


def test_load_unknown_name_is_loud():
    with pytest.raises(ValueError, match="nope"):
        services.load(("nope",))


def test_selected_services_follows_the_profile():
    assert serve.selected_services("da/laptop", ["notebook="]) == (
        "metabase", "jira", "slack", "confluence")


def test_selected_services_without_profile_is_everything():
    assert serve.selected_services(None, []) is None


def test_plug_without_profile_is_an_error():
    with pytest.raises(ProfileError, match="--plug requires --profile"):
        serve.selected_services(None, ["notebook="])


def test_sched_enabled_only_with_ops_sched():
    assert serve.sched_enabled("da/laptop", ["notebook="]) is False
    assert serve.sched_enabled("de/laptop", []) is True
    assert serve.sched_enabled(None, []) is True


def test_main_rejects_planned_profile_before_touching_the_world(tmp_path, capsys):
    audit, world = tmp_path / "audit", tmp_path / "world"
    world.mkdir()
    rc = serve.main(["--audit", str(audit), "--world", str(world),
                     "--profile", "ds/laptop", "--print-only"])
    assert rc == 2
    assert "planned" in capsys.readouterr().err
    assert not audit.exists()


def test_object_store_receiver_follows_the_profile():
    assert serve.object_store_enabled(None, []) is True
    assert serve.object_store_enabled("de/laptop", []) is True
    assert serve.object_store_enabled("da/laptop", ["notebook="]) is False
    assert serve.object_store_enabled("de/laptop", ["object_store="]) is False


# ------------------------------------------------- explicit-only BI vendors
# powerbi, tableau and sigma are plugs a profile has to NAME: an unprofiled
# process loads, mounts and declares exactly the nine it always did.
BI = {"powerbi": 8095, "tableau": 8096, "sigma": 8097}


def test_load_without_names_never_touches_a_bi_vendor():
    loaded, missing = services.load()
    assert not (set(loaded) | set(missing)) & set(services.PLUG_NAMES)


def test_load_names_a_bi_vendor_explicitly():
    for name in services.PLUG_NAMES:
        loaded, missing = services.load((name,))
        assert set(loaded) | set(missing) == {name}


def test_bi_vendor_modules_listen_where_the_table_says():
    # The BI set, exactly. A registry backend (alkera_gateway.backends) is never
    # one of these tables' rows - a hosted one is mounted from its registry row
    # (tasks/harness/test_backends.py holds that rule) - so its name is left out
    # of the comparison rather than made a reason for it to fail.
    from alkera_gateway.backends import BACKENDS

    assert set(services.PLUG_NAMES) - set(BACKENDS) == set(BI)
    assert {n: p for n, p in serve.PLUG_PORTS.items() if n not in BACKENDS} == BI
    # The legacy table is untouched: the host topology awaits every port in it
    # for an unprofiled stack (tasks/harness/native_stack.service_ports).
    assert set(serve.STUB_PORTS) == set(services.NAMES)
    loaded, missing = services.load(tuple(BI))
    for name, module in loaded.items():
        assert (module.NAME, module.HOST, int(module.PORT)) \
            == (name, f"{name}.internal", BI[name])


def test_selected_services_mounts_a_plugged_bi_vendor():
    assert serve.selected_services("da/laptop", ["notebook=", "bi=powerbi"]) == (
        "powerbi", "jira", "slack", "confluence")
    assert "sigma" in serve.selected_services("de/snowflake", [])
    assert "tableau" not in serve.selected_services("de/laptop", [])


def test_bi_vendor_ports_are_known_to_the_stub_and_disagreement_checks():
    assert serve.parse_stub("sigma") == ("sigma", 8097)
    wrong = serve.Mounted("sigma", "sigma.internal", 9999, None, "module", True)
    assert "8097" in serve.port_disagreements([wrong])["sigma"]


def test_print_only_with_a_bi_vendor_declares_its_destination(tmp_path, capsys):
    audit, world = tmp_path / "audit", tmp_path / "world"
    world.mkdir()
    rc = serve.main(["--audit", str(audit), "--world", str(world),
                     "--profile", "da/laptop", "--plug", "notebook=",
                     "--plug", "bi=sigma", "--print-only"])
    assert rc == 0, capsys.readouterr()
    import json

    rows = [json.loads(line) for line in
            (audit / serve.DESTINATIONS).read_text().splitlines()]
    hosts = {(r["host"], r["port"]) for r in rows}
    assert ("sigma.internal", 8097) in hosts
    assert ("metabase.internal", 3000) not in hosts
