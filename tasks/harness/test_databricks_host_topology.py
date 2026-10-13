"""The host topology refuses a Databricks profile, loudly, before anything starts.

Databricks answers on https://databricks.internal:443 (docs/environments/
DATABRICKS_SPIKE.md: the SQL connector reaches no other port), and a host-topology
stack is K rollouts on one loopback interface at a port offset - so the refusal names
the backend and the topology that can run it, instead of a rollout dying at bind or a
subject handed a workspace it cannot reach.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import episode       # noqa: E402
import native_stack  # noqa: E402


def stack(tmp_path: Path, profile: str, plugs=None):
    return native_stack.NativeStack(
        world={}, audit_dir=tmp_path / "audit", rollout_dir=tmp_path / "rollout",
        spec=SimpleNamespace(profile=profile, plugs=plugs or {}))


def test_a_databricks_profile_is_refused_by_name(tmp_path):
    with pytest.raises(episode.HarnessError) as refused:
        stack(tmp_path, "de/databricks")
    message = str(refused.value)
    assert "de/databricks" in message and "databricks.internal:443" in message
    assert "--topology container" in message
    assert not (tmp_path / "rollout").exists()          # refused before anything ran


def test_a_plug_override_is_refused_too(tmp_path):
    with pytest.raises(episode.HarnessError, match="Databricks"):
        stack(tmp_path, "de/laptop", {"warehouse": ["databricks"]})


@pytest.mark.parametrize("profile", ["", "de/laptop", "da/laptop", "de/snowflake"])
def test_other_profiles_construct_as_before(tmp_path, profile):
    assert stack(tmp_path, profile).profile_name == profile
    assert native_stack.host_topology_refusals(None) == []
