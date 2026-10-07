"""The backends a profiled variant gets in the Harbor image, as the exporter renders them.

For each profile whose backend the image serves (variant.HARBOR_BACKENDS): the
variant's Dockerfile passes every lint the template passes and installs what the
backend needs right after its client lock; its episode.json `backends` pass the
environment runtime's own contract checks (and a backend never runs as the agent);
its `statement_logs` likewise; every file its Dockerfile COPYs is a tracked
repository file the context resolves (never under tasks/). The Docker proof of each
is test_env_docker_backends.py.

    gateway/.venv/bin/python -m pytest -q harbor/tests/test_export_backends.py
"""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

import test_env_dockerfile as tdf
import test_export_support  # noqa: F401 - puts harbor/ on sys.path
import test_export_variant as tev
from alkera_harbor import adapter, dockerenv, variant
from alkera_harbor.constants import REPO

sys.path.insert(0, str(REPO / "harbor" / "template" / "environment" / "bin"))
import alkera_runtime as rt  # noqa: E402


def variant_of(profile: str, plugs: dict | None = None) -> variant.Variant:
    return variant.variant_of(SimpleNamespace(profile=profile, plugs=plugs or {}))


def dockerfile(v: variant.Variant) -> str:
    return variant.render_dockerfile(tev.template_text(), v, tev.template_env())


def episode_for(v: variant.Variant) -> dict:
    return {"agent_user": "engineer", "backends": variant.backend_specs(v),
            "statement_logs": variant.statement_logs(v)}


def runs(text: str) -> list[str]:
    return [rest for k, rest in tdf.instructions(text) if k == "RUN"]


def tracked() -> set[str]:
    return set(subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True,
                              text=True, check=True).stdout.split("\n"))


def assert_exportable(v: variant.Variant, tmp_path) -> str:
    """The checks every served backend's variant passes; returns its Dockerfile."""
    text = dockerfile(v)
    tev.test_bi_variant_passes_the_template_lints(text)
    # every network-fetching layer of the unprofiled image is shared (cached)
    steps, template = tdf.instructions(text), tdf.instructions()
    cut = next(i for i, (k, rest) in enumerate(template)
               if k == "RUN" and "INSTALL postgres" in rest) + 1
    assert steps[:cut] == template[:cut]
    episode = episode_for(v)
    assert rt.load_backends(episode) == episode["backends"]
    assert rt.load_statement_logs(episode) == episode["statement_logs"]
    files = tracked()
    for src, dest in v.support_files():
        assert src in files, src
        assert not src.startswith("tasks/"), src
        assert f"alkera/{dest}" in text, f"{dest} is resolved but never COPYed"
    # the adapter resolves the variant's locks and support files together
    exporter = adapter.AlkeraHarborAdapter(output_dir=tmp_path / "x")
    exporter._environment_context(v, "TOOLTOUR", tmp_path / "alkera")
    for src, dest in v.support_files():
        assert (tmp_path / "alkera" / dest).read_bytes() == (REPO / src).read_bytes()
    for rel in v.context_files():
        assert (tmp_path / "alkera" / rel).is_file()
    copies = [rest.split()[0] for k, rest in tdf.instructions(text) if k == "COPY"]
    for source in copies:
        if source.startswith("alkera/"):
            assert (tmp_path / source).exists(), f"COPY {source}: not in the context"
    assert dockerenv.static_agent_env(text)
    return text


# ------------------------------------------------------------------ airflow
def test_airflow_is_hosted_with_its_client(tmp_path):
    v = variant_of("de/laptop", {"orchestrator": ["airflow"]})
    assert v.backends == ("airflow",) and v.harbor_backends == ("airflow",)
    assert v.clients == ("airflow",)
    assert variant.backend_specs(v) == [] and variant.statement_logs(v) == []
    text = assert_exportable(v, tmp_path)
    assert any("requirements-airflow.lock" in r for r in runs(text))
    env = dockerenv.parse_env(text)
    assert env["AIRFLOW_API_URL"] == "http://airflow.internal:8100/api/v2"
    assert "SCHED_URL" not in env
    assert env["REQUESTS_CA_BUNDLE"] == tev.template_env()["REQUESTS_CA_BUNDLE"]


# --------------------------------------------------------------- databricks
def test_databricks_is_hosted_and_the_box_trusts_its_bundle(tmp_path):
    v = variant_of("de/databricks")
    assert v.backends == ("databricks",) and v.clients == ("databricks",)
    assert variant.backend_specs(v) == [] and variant.statement_logs(v) == []
    text = assert_exportable(v, tmp_path)
    env = dockerenv.parse_env(text)
    bundle = "/etc/alkera/databricks-tls/bundle.pem"
    assert env["REQUESTS_CA_BUNDLE"] == env["SSL_CERT_FILE"] == bundle
    assert env["DATABRICKS_HOST"] == "https://databricks.internal"
    lock = next(i for i, r in enumerate(runs(text)) if "requirements-databricks.lock" in r)
    assert "/etc/alkera/databricks-tls" in runs(text)[lock + 1]
    # every non-TLS variant keeps the template's bundle
    laptop = variant_of("de/laptop", {"orchestrator": ["airflow"]})
    assert dockerenv.parse_env(dockerfile(laptop))["REQUESTS_CA_BUNDLE"] \
        == tev.template_env()["REQUESTS_CA_BUNDLE"]


# ----------------------------------------------------------------- redshift
def test_redshift_runs_whole_as_root_with_its_server(tmp_path):
    from alkera_gateway import backends

    v = variant_of("de/aws")
    assert v.backends == ("redshift",) and v.clients == ("redshift",)
    [spec] = variant.backend_specs(v)
    row = backends.BACKENDS["redshift"]
    assert spec["name"] == "redshift" and spec["user"] == "root"
    assert spec["ports"] == [row.port] == [5439] and spec["hosts"] == [row.host]
    assert spec["health"] == "tcp"
    argv = spec["argv"]
    assert argv[0] == "/usr/bin/env" and argv[-2:] == [variant.PYTHON,
                                                       "/opt/stack/redshift_start.py"]
    env = dict(a.split("=", 1) for a in argv[1:-2])
    assert env == {"ALKERA_NATIVE_AUDIT": "/audit/actions.jsonl",
                   "ALKERA_REDSHIFT_SEED": "/world/var/warehouse",
                   "ALKERA_REDSHIFT_DESTINATIONS": "/audit/destinations.redshift.jsonl",
                   "ALKERA_REDSHIFT_STATEMENT_LOG": "/var/log/alkera/redshift-statements.jsonl",
                   "ALKERA_REDSHIFT_DATA": "/var/lib/alkera/redshift"}
    assert variant.statement_logs(v) == []
    text = assert_exportable(v, tmp_path)
    steps = runs(text)
    lock = next(i for i, r in enumerate(steps) if "requirements-redshift.lock" in r)
    assert "postgresql-17=17.11-0+deb13u1" in steps[lock + 1]
    assert "create_main_cluster = false" in steps[lock + 1]
    # the server is the client's build, from the toolbelt's frozen snapshot
    toolbelt = next(r for r in steps if "postgresql-client-17=" in r)
    assert "postgresql-client-17=17.11-0+deb13u1" in toolbelt
    assert v.support_files() == [("gateway/docker/redshift_start.py",
                                  "gateway/docker/redshift_start.py")]


def test_an_unknown_backend_is_refused():
    v = variant.Variant(role="de", profile="de/x", backends=("nope",),
                        harbor_backends=("nope",))
    with pytest.raises(variant.VariantError, match="neither runs nor hosts"):
        variant.backend_specs(v)
