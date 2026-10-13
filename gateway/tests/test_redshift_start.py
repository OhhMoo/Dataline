"""gateway/docker/redshift_start.py's door: one login role through the proxy.

The proxy reaches the server over its private socket, where the superuser is
trusted for provisioning, so a relayed `user=postgres` startup would be a
superuser session (COPY ... TO PROGRAM runs commands as the server's OS user -
in the Harbor image, inside the agent's own container). The start script's
startup filter refuses any user but the cluster's, by name.

    gateway/.venv/bin/python -m pytest -q gateway/tests/test_redshift_start.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from alkera_gateway import redshift_sql

DOCKER = Path(__file__).resolve().parents[1] / "docker"


def _start():
    spec = importlib.util.spec_from_file_location("redshift_start_for_tests",
                                                  DOCKER / "redshift_start.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("user", ["postgres", "POSTGRES", "episode", "root", "", None])
def test_only_the_clusters_user_may_log_in(user):
    start = _start()
    params = {"user": user, "database": "dev"} if user is not None else {"database": "dev"}
    with pytest.raises(redshift_sql.Unsupported, match="logging in as") as refused:
        start.startup_filter(params)
    assert refused.value.sqlstate == "0A000"
    assert "Redshift feature not available in this environment" in str(refused.value)


def test_the_clusters_user_passes_the_personas_own_filter():
    start = _start()
    params = {"user": start.USER, "database": "dev", "options": "-c search_path=warehouse"}
    assert start.startup_filter(dict(params)) == redshift_sql.startup_parameters(dict(params))
    with pytest.raises(redshift_sql.Unsupported):
        start.startup_filter({"user": start.USER, "client_encoding": "LATIN1"})


def test_the_proxy_is_served_with_this_filter(monkeypatch):
    from alkera_gateway.native import pgwire_proxy

    start = _start()
    seen = {}
    monkeypatch.setattr(pgwire_proxy, "serve", lambda *a, **kw: seen.update(kw))
    monkeypatch.delenv(start.DESTINATIONS_ENV, raising=False)
    monkeypatch.delenv(start.STATEMENT_LOG_ENV, raising=False)
    start.serve_proxy()
    assert seen["startup_filter"] is start.startup_filter
