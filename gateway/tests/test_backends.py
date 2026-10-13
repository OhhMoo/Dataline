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


# ----------------------------------------------- a hosted backend in serve.py
def _hosted(monkeypatch, name="hx", port=8123, module_port=None):
    """A synthetic hosted row, its catalogue plug and its service module."""
    import sys
    import types

    from alkera_gateway import plugs

    row = Backend(name, port=port, scheme="https", tls=True,
                  env={"HX_HOST": f"https://{name}.internal:{port}"})
    monkeypatch.setitem(BACKENDS, name, row)
    monkeypatch.setitem(plugs.PLUGS, name, plugs.Plug(name, "warehouse", "partial"))
    module = types.ModuleType(f"alkera_gateway.native.services.{name}")

    async def app(scope, receive, send):           # never called here
        raise AssertionError("not served in this test")

    module.NAME, module.HOST, module.PORT = name, f"{name}.internal", module_port or port
    module.app = app
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return row, module


def _declared(tmp_path, name, argv) -> set[tuple[str, int]]:
    import json

    from alkera_gateway.native import serve

    audit, world = tmp_path / name / "audit", tmp_path / name / "world"
    world.mkdir(parents=True)
    rc = serve.main(["--audit", str(audit), "--world", str(world), "--print-only", *argv])
    assert rc == 0
    return {(r["host"], r["port"]) for r in map(
        json.loads, (audit / serve.DESTINATIONS).read_text().splitlines())}


def test_hosted_backend_port_matches_serve(tmp_path, monkeypatch):
    """A hosted row's module is mounted on the row's port - not a table's,
    not the module's own - and only when a profile plugs it; an unprofiled
    serve binds exactly the ports it bound before the row existed."""
    from alkera_gateway.native import serve, services

    before = _declared(tmp_path, "before", [])
    row, module = _hosted(monkeypatch, module_port=1)      # the module disagrees
    plugged = serve.selected_services("da/laptop", ["notebook=", "warehouse=hx"])
    assert plugged == (*serve.selected_services("da/laptop", ["notebook="]), "hx")
    assert "hx" not in serve.selected_services("da/laptop", ["notebook="])
    assert services.load() == services.load(services.NAMES)      # never by default
    loaded, missing = services.load(plugged)
    assert loaded["hx"] is module and not missing.get("hx")
    hx = {m.name: m for m in serve.mount(loaded)}["hx"]
    assert (hx.host, hx.port) == (row.host, row.port)
    assert serve.port_disagreements([hx]) == {}
    # exactly the registry's hosted rows - hx and whichever real ones exist
    assert serve.hosted_ports() == {n: b.port for n, b in BACKENDS.items() if b.hosted}
    assert serve.hosted_ports()["hx"] == 8123 and serve.service_ports()["hx"] == 8123
    declared = _declared(tmp_path, "plugged",
                         ["--profile", "da/laptop", "--plug", "notebook=",
                          "--plug", "warehouse=hx"])
    assert ("hx.internal", 8123) in declared
    assert _declared(tmp_path, "unprofiled", []) == before
    assert not any(port == 8123 for _, port in before)
    # a second TLS backend in the same profile is refused before anything binds
    with pytest.raises(ProfileError, match="'snowflake', 'hx'"):
        serve.selected_services("da/laptop", ["notebook=", "warehouse=snowflake,hx"])


def _hosted_problems(name: str, row: Backend, module) -> list[str]:
    """What is wrong with a hosted row and its service module: the module
    must name the row's NAME/HOST/PORT, and the row's port must be free in the
    one serve.py process it is mounted in."""
    from alkera_gateway.native import serve

    problems = []
    for attr, want in (("NAME", name), ("HOST", row.host), ("PORT", row.port)):
        got = getattr(module, attr, None)
        if (int(got) if attr == "PORT" and got is not None else got) != want:
            problems.append(f"{attr} {got!r} != row's {want!r}")
    taken = {**{f"stub {n}": p for n, p in serve.STUB_PORTS.items()},
             **{f"bi {n}": p for n, p in serve.PLUG_PORTS.items()},
             "aggregate": serve.AGGREGATE_PORT, "sched": serve.SCHED_PORT,
             "minio receiver": serve.MINIO_AUDIT_PORT,
             **{f"hosted {n}": b.port for n, b in BACKENDS.items()
                if b.hosted and n != name}}
    problems += [f"port {row.port} is {owner}'s" for owner, port in taken.items()
                 if port == row.port]
    return problems


HOSTED_ROWS = sorted(n for n, b in BACKENDS.items() if b.hosted)


@pytest.mark.parametrize("name", HOSTED_ROWS or [pytest.param(None, id="no-hosted-row-yet")])
def test_every_hosted_row_has_its_module_on_a_free_port(name):
    """Each hosted row: `native/services/<name>.py` exists and is mounted where
    the row says, on a port nothing else in serve.py takes. Vacuous until Wave 1
    adds the first real row."""
    if name is None:
        return
    from alkera_gateway.native import services

    path = GATEWAY / "src" / "alkera_gateway" / "native" / "services" / f"{name}.py"
    assert path.is_file(), path
    loaded, missing = services.load((name,))
    assert name in loaded, missing
    assert _hosted_problems(name, BACKENDS[name], loaded[name]) == []


def test_the_hosted_row_check_catches_what_it_should():
    import types

    def module(name, host, port):
        m = types.ModuleType(name)
        m.NAME, m.HOST, m.PORT = name, host, port
        return m

    good = Backend("hx", port=8123)
    assert _hosted_problems("hx", good, module("hx", "hx.internal", 8123)) == []
    assert _hosted_problems("hx", good, module("hx", "hx.internal", 8124)) == [
        "PORT 8124 != row's 8123"]
    for port, owner in ((8099, "aggregate"), (8120, "sched"), (8110, "minio receiver"),
                        (8080, "stub jira"), (8097, "bi sigma")):
        clash = Backend("hx", port=port)
        assert _hosted_problems("hx", clash, module("hx", "hx.internal", port)) == [
            f"port {port} is {owner}'s"], port


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


@pytest.mark.parametrize("private", ["trust", "trust/private", "trust/a/b"])
def test_mint_refuses_a_key_inside_the_trust_dir(tmp_path, private):
    with pytest.raises(ValueError, match="inside trust_dir"):
        tls.mint("x.internal", tmp_path / "trust", private_dir=tmp_path / private)
    assert not (tmp_path / "trust").exists()


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
    assert "RUN set -euf;" in loop                     # a client name is never a glob
    assert "python /opt/gateway/registry/warehouse_clients.py" in loop
    assert 'for c in $(echo "$ALKERA_WAREHOUSE_CLIENTS" | tr \',\' \' \')' in loop
    assert 'lock="/opt/gateway/clients/requirements-$c.lock"' in loop
    assert '--require-hashes -r "$lock"' in loop
    assert "rm -rf /opt/gateway/clients /opt/gateway/registry" in loop
    assert "snowflake" not in loop               # no client set is special-cased


def _client_loop(tmp_path) -> tuple[str, dict, Path]:
    """The Dockerfile's client RUN line, re-rooted under tmp_path with its COPY
    sources staged and pip stubbed: (script, environment, staging dir)."""
    import shutil
    import sys

    text = (DOCKER / "Dockerfile").read_text(encoding="utf-8")
    run = text[text.index("RUN set -euf;", text.index('ARG ALKERA_WAREHOUSE_CLIENTS=""')):]
    run = run[len("RUN "):run.index("\nLABEL")].replace("\\\n", "\n")
    root = tmp_path / "opt" / "gateway"
    registry = root / "registry" / "alkera_gateway"
    registry.mkdir(parents=True)
    shutil.copy(DOCKER / "warehouse_clients.py", root / "registry")
    for name in ("plugs.py", "backends.py"):
        shutil.copy(GATEWAY / "src" / "alkera_gateway" / name, registry)
    staged = root / "clients"
    staged.mkdir()
    for name in ("requirements-snowflake.lock", "requirements-ds.lock",
                 "requirements-snowflake-extra.lock"):
        (staged / name).write_text("x\n")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "pip").write_text('#!/bin/sh\necho "pip $*" >> "$PIPLOG"\n')
    (tmp_path / "bin" / "pip").chmod(0o755)
    (tmp_path / "bin" / "python").symlink_to(sys.executable)
    env = {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "PIPLOG": str(tmp_path / "pip.log")}
    return run.replace("/opt/gateway", str(root)), env, staged


@pytest.mark.parametrize("clients,installed", [
    ("", []), ("snowflake", ["snowflake"]), ("snowflake,", ["snowflake"])])
def test_dockerfile_client_loop_runs_under_sh(tmp_path, clients, installed):
    """The RUN line itself, under /bin/sh, with pip stubbed."""
    script, env, staged = _client_loop(tmp_path)
    subprocess.run(["sh", "-c", script], check=True, capture_output=True,
                   env={**env, "ALKERA_WAREHOUSE_CLIENTS": clients})
    log = (tmp_path / "pip.log").read_text() if (tmp_path / "pip.log").exists() else ""
    assert [line.rsplit("requirements-", 1)[1][:-len(".lock")]
            for line in log.splitlines()] == installed
    assert not staged.exists() and not (staged.parent / "registry").exists()
    kept = sorted(p.name for p in staged.parent.glob("requirements-*.lock"))
    assert kept == [f"requirements-{c}.lock" for c in installed]


@pytest.mark.parametrize("bad", ["ds", "native", "nope", "*", "snow*", "snowflake-extra"])
def test_dockerfile_client_loop_refuses_what_the_registry_does_not_name(tmp_path, bad):
    script, env, _ = _client_loop(tmp_path)
    done = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                          env={**env, "ALKERA_WAREHOUSE_CLIENTS": bad})
    assert done.returncode != 0 and bad in done.stderr and "registry" in done.stderr
    assert not (tmp_path / "pip.log").exists()


def test_build_episode_clients_come_from_the_registry():
    build = _load("build_episode")
    assert build.WAREHOUSE_CLIENTS == tuple(sorted(
        {b.clients for b in BACKENDS.values() if b.clients}))
    for c in build.WAREHOUSE_CLIENTS:
        assert build.image_tag("alkera-episode:head", "da", c) == f"alkera-episode:head-da-{c}"
    with pytest.raises(SystemExit, match="unknown warehouse client"):
        build.clients("nope")


def test_build_episode_reads_the_registry_on_a_plain_python(tmp_path):
    """build_episode.py runs on a plain `python` (its docstring, gateway/README,
    TOOLBELT.md): its registry loader needs neither the gateway installed nor
    the package `__init__` (pydantic, duckdb). Run isolated (-I) with no site
    packages (-S), so no gateway is on sys.path at all."""
    import sys

    base = Path(sys.base_prefix) / "bin" / "python3"
    code = ("import runpy, sys; m = runpy.run_path(sys.argv[1], run_name='build');"
            "print(','.join(m['WAREHOUSE_CLIENTS']));"
            "bad = [n for n in sys.modules if n.split('.')[0] in ('alkera_gateway', 'pydantic')];"
            "assert not bad, bad")
    done = subprocess.run([str(base), "-I", "-S", "-c", code,
                           str(DOCKER / "build_episode.py")],
                          capture_output=True, text=True, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ",".join(_load("build_episode").WAREHOUSE_CLIENTS)
    usage = subprocess.run([str(base), "-I", "-S", str(DOCKER / "build_episode.py"), "--help"],
                           capture_output=True, text=True, cwd=tmp_path)
    assert usage.returncode == 0 and "--warehouse-clients" in usage.stdout
