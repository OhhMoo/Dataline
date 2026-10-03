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
