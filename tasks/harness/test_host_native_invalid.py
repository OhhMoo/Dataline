"""Host-native runs cannot publish a Pass@1.

    python -m pytest test_host_native_invalid.py -q

`--driver native --topology host` already prints HOST_TOPOLOGY_WARNING, but
the report used to still emit Pass@1. These checks pin that any native+host
record invalidates the rate with the token host_native_not_contained.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report  # noqa: E402

TOKEN = "host_native_not_contained"


def rec(*, driver: str, topology: str | None = "host") -> dict:
    """Minimum record `_run_conditions` needs without other invalidations."""
    r = {
        "driver": driver,
        "surface_audit": {
            "live": ["sql"],
            "dead": [],
            "gate": "pass",
            "declared_probes": 1,
            "swept_probes": 1,
        },
    }
    if topology is not None:
        r["topology"] = topology
    return r


def test_native_host_invalidates():
    c = report._run_conditions([rec(driver="native", topology="host")])
    assert c["measurement_valid"] is False
    assert any(TOKEN in x for x in c["invalidated_because"])


def test_native_container_does_not_use_this_token():
    c = report._run_conditions([rec(driver="native", topology="container")])
    assert all(TOKEN not in x for x in c["invalidated_because"])


def test_mcp_host_does_not_use_this_token():
    c = report._run_conditions([rec(driver="mcp", topology="host")])
    assert all(TOKEN not in x for x in c["invalidated_because"])


def test_native_missing_topology_counts_as_host():
    c = report._run_conditions([rec(driver="native", topology=None)])
    assert c["measurement_valid"] is False
    assert any(TOKEN in x for x in c["invalidated_because"])
