"""The backend registry (alkera_gateway.backends) and what it is built from.

The harness side - the overlay a plugged backend gets, its endpoint, the TLS
refusal at overlay time - is tasks/harness/test_backends.py. Here: the rows
themselves, the shared TLS helper the Snowflake container now calls, the
generic client-lock script, and the Dockerfile and build script that install a
backend's clients. No docker, no network.
"""
from __future__ import annotations

import ast
import importlib.util
import subprocess
from pathlib import Path

import pytest

from alkera_gateway import backends
from alkera_gateway.backends import BACKENDS, Backend, plugged
from alkera_gateway.native import tls
from alkera_gateway.plugs import PLUGS, ProfileError

GATEWAY = Path(__file__).resolve().parent.parent
DOCKER = GATEWAY / "docker"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_for_backends", DOCKER / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------- the rows
def test_snowflake_row_is_the_service_as_it_runs():
    sf = BACKENDS["snowflake"]
    assert (sf.host, sf.port, sf.scheme) == ("snowflake.internal", 8443, "https")
    assert not sf.hosted and sf.dockerfile == "Dockerfile.snowflake"
    assert (DOCKER / sf.dockerfile).is_file()
    assert sf.clients == "snowflake" and sf.seeds_world and sf.tls
    assert sf.statement_tool == "snowflake.statement"
    assert backends.endpoint(sf) == "https://snowflake.internal:8443"
    assert backends.destinations_file(sf) == "destinations.snowflake.jsonl"
    assert backends.bundle(sf) == "/etc/alkera/snowflake-tls/bundle.pem"
    # the variables the unprofiled static table already carries for it, and
    # the database the seed makes of the world's warehouse.duckdb
    assert set(sf.env) == {"SNOWFLAKE_HOST", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER",
                           "SNOWFLAKE_PASSWORD", "SNOWFLAKE_DATABASE"}
    assert sf.env["SNOWFLAKE_HOST"] == sf.host
    assert sf.env["SNOWFLAKE_DATABASE"] == "WAREHOUSE"


def test_every_row_is_a_plug_and_complete():
    for name, b in BACKENDS.items():
        assert b.name == name and name in PLUGS, name
        assert b.port > 0 and b.host == f"{name}.internal", name
        assert b.hosted == (b.dockerfile is None), name
        if b.dockerfile:
            assert (DOCKER / b.dockerfile).is_file(), name
        if b.clients:
            assert (DOCKER / f"{b.clients}-clients.in").is_file(), name
            assert (GATEWAY / f"requirements-{b.clients}.lock").is_file(), name
        if b.statement_tool:
            assert b.statement_tool == f"{name}.statement", name


def test_a_row_is_immutable_and_defaults_its_host():
    b = Backend("x", tls=True)
    assert b.host == "x.internal" and b.env == {}
    with pytest.raises(TypeError):
        b.env["X"] = "y"                         # read-only mapping


def test_plugged_reads_every_slot_in_registry_order():
    assert plugged({}) == () and plugged(None) == ()
    assert plugged({"warehouse": ("duckdb",), "bi": ("sigma",)}) == ()
    assert plugged({"warehouse": ("duckdb", "snowflake")}) == (BACKENDS["snowflake"],)


def test_two_tls_backends_name_both(monkeypatch):
    monkeypatch.setitem(BACKENDS, "x", Backend("x", port=1, tls=True))
    monkeypatch.setitem(BACKENDS, "y", Backend("y", port=2))
    with pytest.raises(ProfileError, match="'snowflake', 'x'"):
        plugged({"warehouse": ("snowflake",), "orchestrator": ("x",)})
    # one TLS backend beside a plain one is fine
    assert [b.name for b in plugged({"warehouse": ("snowflake", "y")})] == ["snowflake", "y"]


def test_registry_module_is_dependency_free():
    tree = ast.parse((GATEWAY / "src" / "alkera_gateway" / "backends.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or ""))
    assert imported == {"__future__", "collections.abc", "dataclasses", "types",
                        "typing", ".plugs"}, imported


def test_dbt_output_from_the_boxs_variables():
    from alkera_gateway.warehouses import dbt_output

    out = dbt_output("snowflake", schema="PUBLIC", port=8443,
                     env={"account": "SNOWFLAKE_ACCOUNT", "database": "SNOWFLAKE_DATABASE"})
    assert out == {"type": "snowflake", "account": "{{ env_var('SNOWFLAKE_ACCOUNT') }}",
                   "database": "{{ env_var('SNOWFLAKE_DATABASE') }}", "port": 8443,
                   "schema": "PUBLIC", "threads": 1}
    # without `env` the gateway's own secret is read, as before
    assert "ALKERA_SNOWFLAKE_OPTIONS" in dbt_output("snowflake", schema="s")["account"]
    for backend in ("duckdb", "nope"):
        with pytest.raises(ValueError, match=backend):
            dbt_output(backend, schema="s", env={"host": "H"})


# --------------------------------------------------------------- native.tls
def _x509(path: Path) -> str:
    return subprocess.run(["openssl", "x509", "-in", str(path), "-noout", "-text"],
                          check=True, capture_output=True, text=True).stdout


def test_mint_writes_key_cert_and_bundle(tmp_path):
    import certifi

    crt, key = tls.mint("x.internal", tmp_path / "trust", private_dir=tmp_path / "private")
    assert (crt, key) == (tmp_path / "private" / "server.crt",
                          tmp_path / "private" / "server.key")
    assert key.stat().st_mode & 0o777 == 0o600
    trust = tmp_path / "trust"
    assert sorted(p.name for p in trust.iterdir()) == ["bundle.pem", "server.crt"]
    assert (trust / "server.crt").read_bytes() == crt.read_bytes()
    assert (trust / "bundle.pem").read_bytes() == \
        Path(certifi.where()).read_bytes() + b"\n" + crt.read_bytes()
    text = _x509(crt)
    assert "CN=x.internal" in text or "CN = x.internal" in text
    assert "DNS:x.internal, DNS:localhost, IP Address:127.0.0.1" in text


def test_snowflake_start_mints_the_certificate_it_always_had():
    start = _load("snowflake_start")
    # the subjectAltName the pre-registry certificate() passed to openssl
    assert tls.subject_alt_names(start.HOSTNAME, start.ALT_NAMES) == \
        start.SUBJECT_ALT_NAMES == ("DNS:acme.snowflake.local,DNS:*.snowflake.local,"
                                    "DNS:snowflake.internal,DNS:localhost,IP:127.0.0.1")
    assert (start.TLS, start.TRUST) == (Path("/tls"), Path("/etc/alkera/snowflake-tls"))
    assert str(start.TRUST) == backends.trust_dir(BACKENDS["snowflake"])
    assert "mint(HOSTNAME, TRUST, private_dir=TLS, alt_names=ALT_NAMES)" in \
        (DOCKER / "snowflake_start.py").read_text()


# ------------------------------------------------------- client locks, image
def test_lock_script_is_generic_and_keeps_snowflakes_header():
    lock = _load("lock_warehouse_clients")
    assert lock.DIVERGES == {"certifi"}
    assert lock.CONSTRAINED_BY == ("requirements.lock", "requirements-native.lock",
                                   "requirements-ds.lock")
    pins = lock.constraints()
    assert "certifi==" not in pins and "sqlglot==30.18.0" in pins
    committed = (GATEWAY / "requirements-snowflake.lock").read_text().splitlines(True)
    assert lock.header("snowflake") == "".join(committed[:3])
    assert lock.header("redshift").splitlines()[1] == \
        "#    gateway/.venv/bin/python gateway/docker/lock_warehouse_clients.py redshift"
    argv = lock.compile_argv("snowflake", True, Path("out.lock"))
    assert argv[:4] == ["uv", "pip", "compile", "snowflake-clients.in"]
    assert ["--constraint", "base-pins.txt"] == argv[argv.index("--constraint"):][:2]
    assert "--overrides" not in lock.compile_argv("x", False, Path("o"))
    with pytest.raises(SystemExit, match="no-such-clients.in"):
        lock.sources("no-such")


def test_lock_snowflake_clients_is_the_generic_script():
    text = (DOCKER / "lock_snowflake_clients.py").read_text()
    assert 'lock("snowflake")' in text and "uv" not in text.split('"""', 2)[2]


def test_dockerfile_installs_each_requested_client_lock():
    text = (DOCKER / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY gateway/requirements-*.lock /opt/gateway/clients/" in text
    loop = text[text.index('ARG ALKERA_WAREHOUSE_CLIENTS=""'):text.index(
        "LABEL org.alkera.warehouse_clients")]
    assert 'for c in $(echo "$ALKERA_WAREHOUSE_CLIENTS" | tr \',\' \' \')' in loop
    assert 'lock="/opt/gateway/clients/requirements-$c.lock"' in loop
    assert '--require-hashes -r "$lock"' in loop
    assert "rm -rf /opt/gateway/clients" in loop
    assert "snowflake" not in loop               # no client set is special-cased


@pytest.mark.parametrize("clients,installed", [
    ("", []), ("snowflake", ["snowflake"]), ("snowflake,", ["snowflake"])])
def test_dockerfile_client_loop_runs_under_sh(tmp_path, clients, installed):
    """The RUN line itself, under /bin/sh, with pip stubbed."""
    text = (DOCKER / "Dockerfile").read_text(encoding="utf-8")
    run = text[text.index("RUN set -eu;", text.index('ARG ALKERA_WAREHOUSE_CLIENTS=""')):]
    run = run[len("RUN "):run.index("\nLABEL")].replace("\\\n", "\n")
    staged = tmp_path / "opt" / "gateway" / "clients"
    staged.mkdir(parents=True)
    for name in ("requirements-snowflake.lock", "requirements-ds.lock"):
        (staged / name).write_text("x\n")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "pip").write_text('#!/bin/sh\necho "pip $*" >> "$PIPLOG"\n')
    (tmp_path / "bin" / "pip").chmod(0o755)
    script = run.replace("/opt/gateway", str(tmp_path / "opt" / "gateway"))
    env = {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "PIPLOG": str(tmp_path / "pip.log"),
           "ALKERA_WAREHOUSE_CLIENTS": clients}
    subprocess.run(["sh", "-c", script], check=True, env=env, capture_output=True)
    log = (tmp_path / "pip.log").read_text() if (tmp_path / "pip.log").exists() else ""
    assert [line.rsplit("requirements-", 1)[1][:-len(".lock")]
            for line in log.splitlines()] == installed
    assert not staged.exists()
    kept = sorted(p.name for p in (tmp_path / "opt" / "gateway").glob("requirements-*.lock"))
    assert kept == [f"requirements-{c}.lock" for c in installed]
    for bad in ("ds", "nope"):
        env["ALKERA_WAREHOUSE_CLIENTS"] = bad
        staged.mkdir(parents=True, exist_ok=True)
        (staged / "requirements-ds.lock").write_text("x\n")
        done = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True)
        assert done.returncode != 0 and bad in done.stderr


def test_build_episode_clients_come_from_the_registry():
    build = _load("build_episode")
    assert build.WAREHOUSE_CLIENTS == tuple(sorted(
        {b.clients for b in BACKENDS.values() if b.clients}))
    for c in build.WAREHOUSE_CLIENTS:
        assert build.image_tag("alkera-episode:head", "da", c) == f"alkera-episode:head-da-{c}"
    with pytest.raises(SystemExit, match="unknown warehouse client"):
        build.clients("nope")
