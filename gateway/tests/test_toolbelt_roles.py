"""Per-role images: the box an agent gets is chosen by its role, by what is installed.

Environment profiles are `<role>/<stack>` with roles de, ds and da
(`alkera_gateway.plugs.ROLES`). A tool a role does not get must be NOT INSTALLED in
that role's image - a PATH mask is a fixture the agent sees through with one
`pip list`. Two places hold packages, and these tests read the locks for both:

    /opt/toolbelt venv   CLIs (aws, sqlfluff, csvkit). Same for every role:
                         gateway/docker/toolbelt.lock / toolbelt.arm64.lock
    system python        the gateway (requirements.lock + requirements-native.lock),
                         plus, for ds and da only, the role's libraries:
                         ds  gateway/requirements-ds.lock   (notebook + ds extras)
                         da  gateway/requirements-da.lock   (the same export with
                                                             polars, sklearn pruned)

The role libraries live in the SYSTEM interpreter because they are what the agent's
own `python` and the Jupyter kernel (`docker/jupyter/kernels/alkera-python`, argv
`/usr/local/bin/python`) import, next to `alkera_gateway.native.notebook`. They are
exported from the one gateway/uv.lock, so they agree with the gateway's own pins by
construction - the botocore-style conflict that keeps awscli in a venv cannot arise.

Everything here is offline except the `test_*_wheel_for_its_arch` tests, which read
PyPI's JSON API to map the locks' hashes to wheel filenames and skip - naming why -
when PyPI cannot be reached.
"""

from __future__ import annotations

import importlib.util
import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path

import pytest
from packaging.markers import Marker

GATEWAY = Path(__file__).resolve().parents[1]
DOCKER = GATEWAY / "docker"

ROLES = ("de", "ds", "da")
ARCHES = {"amd64": "x86_64", "arm64": "aarch64"}
BASE_LOCKS = ("requirements.lock", "requirements-native.lock")
ROLE_LOCKS = {"ds": "requirements-ds.lock", "da": "requirements-da.lock"}

# The task's definition of each role, independent of the files that implement it.
# jupyter-server is in da too: the da profile has the `notebook` slot, whose plug IS
# a jupyter-server (docker/jupyter/launch.py).
ADDED = {
    "de": set(),
    "ds": {"pandas", "pyarrow", "polars", "scikit-learn", "matplotlib", "ipykernel",
           "jupyter-client", "jupyter-server", "nbclient", "nbformat"},
    "da": {"pandas", "pyarrow", "matplotlib", "ipykernel", "jupyter-client",
           "jupyter-server", "nbclient", "nbformat"},
}
DS_ONLY = ADDED["ds"] - ADDED["da"]

# Packages that ship platform wheels and so need one per arch.
COMPILED = {"numpy", "pandas", "pyarrow", "polars-runtime-32", "scikit-learn", "scipy",
            "matplotlib", "pyzmq", "tornado", "psutil", "kiwisolver", "pillow",
            "contourpy"}

# Locked as an sdist on purpose, with the reason. Anything else sdist-only fails.
SDIST_OK = {
    # Its PEP 517 backend downloads the prebuilt wheel for the platform from the
    # dbt-core GitHub release and checks a sha256 embedded in the sdist; the manifest
    # lists manylinux_2_28_x86_64 and manylinux_2_28_aarch64. Via dbt-core.
    "dbt-core-experimental-parser",
}

_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)(?:\s*;\s*([^\\]+?))?\s*\\?$")
_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def environment(arch: str) -> dict[str, str]:
    """The episode image's interpreter: CPython 3.11 on glibc Linux."""
    return {"python_version": "3.11", "python_full_version": "3.11.13",
            "sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
            "platform_machine": ARCHES[arch], "implementation_name": "cpython",
            "platform_python_implementation": "CPython", "extra": ""}


def parse_lock(path: Path, arch: str = "amd64") -> dict[str, tuple[str, set[str]]]:
    """{name: (version, {sha256, ...})} for the entries pip would install on `arch`.

    uv export writes a universal lock: one name can appear twice under different
    markers (numpy 2.4.6 for <3.12, 2.5.3 for >=3.12). Markers are evaluated, so
    the answer is the image's, not the last line's.
    """
    pins: dict[str, tuple[str, set[str]]] = {}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _REQ.match(line)
        if match:
            name, version, marker = match.groups()
            if marker and not Marker(marker).evaluate(environment(arch)):
                current = None
                continue
            current = norm(name)
            assert current not in pins, f"{path.name}: {current} pinned twice for {arch}"
            pins[current] = (version, set(_HASH.findall(line)))
        elif current and line.startswith("    "):
            pins[current][1].update(_HASH.findall(line))
        elif line and not line.startswith(" "):
            current = None
    return pins


def versions(path: Path, arch: str = "amd64") -> dict[str, str]:
    return {k: v for k, (v, _) in parse_lock(path, arch).items()}


def header_command(path: Path) -> str:
    return path.read_text(encoding="utf-8").splitlines()[1].lstrip("# ").strip()


def installed(role: str, arch: str = "amd64") -> dict[str, str]:
    """Everything the system interpreter gets for a role (snowflake clients aside)."""
    out: dict[str, str] = {}
    for name in (*BASE_LOCKS, *([ROLE_LOCKS[role]] if role in ROLE_LOCKS else [])):
        out.update(versions(GATEWAY / name, arch))
    return out


# --- the toolbelt venv: one lock, both arches, every role ------------------------------

@pytest.mark.parametrize("arch", ARCHES)
def test_toolbelt_venv_lock_was_compiled_for_its_arch(arch):
    name = "toolbelt.lock" if arch == "amd64" else "toolbelt.arm64.lock"
    command = header_command(DOCKER / name)
    assert command.startswith("uv pip compile docker/toolbelt.in --generate-hashes")
    assert "--python-version 3.11" in command
    assert f"--python-platform {ARCHES[arch]}-unknown-linux-gnu" in command
    assert command.endswith(f"--output-file docker/{name}")


def test_toolbelt_venv_locks_pin_the_same_versions_on_both_arches():
    assert versions(DOCKER / "toolbelt.lock") == versions(DOCKER / "toolbelt.arm64.lock")


def test_metricflow_in_the_toolbelt_venv_shares_the_gateways_dbt_and_duckdb():
    """`mf` (dbt-metricflow) runs its own dbt parse and opens the warehouse FILE
    with its own DuckDB. Pinned to the gateway's (system interpreter) dbt-core,
    dbt-duckdb and duckdb, so `mf` parses the project the way the box's `dbt` does
    and never writes the warehouse in a storage format the world's DuckDB cannot
    read. csvkit 2.0.1 is the price: csvkit>=2.1 needs agate>=1.12 and every
    dbt-common pins agate<1.10 (TOOLBELT.md)."""
    venv, gateway = versions(DOCKER / "toolbelt.lock"), versions(GATEWAY / "requirements.lock")
    assert venv["dbt-metricflow"] == "0.15.0" and venv["metricflow"] == "0.213.0"
    for name in ("dbt-core", "dbt-duckdb", "duckdb"):
        assert venv[name] == gateway[name], name
    assert venv["csvkit"] == "2.0.1" and venv["agate"].startswith("1.9.")


def test_role_libraries_are_not_in_the_toolbelt_venv():
    """They would be invisible there: `python` is /usr/local/bin/python."""
    assert not (ADDED["ds"] & set(versions(DOCKER / "toolbelt.lock")))


def test_there_are_no_per_role_venv_locks():
    """ds and da add no CLI to the venv, so a per-role venv lock would be de's twice."""
    assert not list(DOCKER.glob("toolbelt-*.lock")) and not list(DOCKER.glob("toolbelt-*.in"))


# --- the system interpreter: exports of the one uv.lock --------------------------------

@pytest.mark.parametrize("role,extra", [
    ("ds", "--extra notebook --extra ds"),
    ("da", "--extra notebook --extra ds --prune polars --prune scikit-learn"),
])
def test_role_lock_is_an_export_of_uv_lock(role, extra):
    command = header_command(GATEWAY / ROLE_LOCKS[role])
    assert command == (f"uv export --locked {extra} --no-dev --no-emit-project "
                       f"--format requirements-txt --output-file {ROLE_LOCKS[role]}")


@pytest.mark.parametrize("arch", ARCHES)
@pytest.mark.parametrize("role", ROLES)
def test_each_role_installs_exactly_its_additions(role, arch):
    got = installed(role, arch)
    assert ADDED[role] <= set(got), sorted(ADDED[role] - set(got))
    forbidden = (ADDED["ds"] | ADDED["da"]) - ADDED[role]
    assert not forbidden & set(got), f"{role} would install {sorted(forbidden & set(got))}"


def test_da_drops_the_ds_only_closure_not_just_the_names():
    ds, da = set(versions(GATEWAY / ROLE_LOCKS["ds"])), set(versions(GATEWAY / ROLE_LOCKS["da"]))
    assert da < ds
    assert {"scipy", "polars-runtime-32", "joblib", "threadpoolctl"} <= ds - da


@pytest.mark.parametrize("arch", ARCHES)
@pytest.mark.parametrize("role", ("ds", "da"))
def test_role_lock_agrees_with_the_base_locks(role, arch):
    """The role lock is installed AFTER requirements.lock + requirements-native.lock.
    Every package they share (the gateway's base dependencies: duckdb, pydantic, ...)
    must carry one version, so pip finds it already satisfied and a role never
    moves the gateway's own environment."""
    role_pins = versions(GATEWAY / ROLE_LOCKS[role], arch)
    assert {"duckdb", "pydantic"} <= set(role_pins), "exported with the gateway's base deps"
    for base in BASE_LOCKS:
        base_pins = versions(GATEWAY / base, arch)
        clash = {k: (role_pins[k], base_pins[k]) for k in set(role_pins) & set(base_pins)
                 if role_pins[k] != base_pins[k]}
        assert not clash, (base, clash)


def test_role_versions_match_the_pyproject_extras_resolution():
    """pandas 3 / pyarrow 25 / ... - what uv.lock resolved, so the kernel and the
    gateway's own notebook tests mean the same thing by each name."""
    ds = versions(GATEWAY / ROLE_LOCKS["ds"])
    assert ds["pandas"].startswith("3.") and ds["ipykernel"].startswith("7.")
    assert ds["numpy"] == "2.4.6", "the <3.12 branch of the universal lock"


@pytest.mark.parametrize("role", ("ds", "da"))
def test_compiled_packages_carry_all_arch_hashes(role):
    """Offline proxy for the PyPI check below: a compiled package with one hash was
    locked for one platform wheel and cannot install on the other arch."""
    locked = parse_lock(GATEWAY / ROLE_LOCKS[role], "arm64")
    present = COMPILED & set(locked)
    thin = {n: len(locked[n][1]) for n in present if len(locked[n][1]) < 4}
    assert not thin
    assert {"numpy", "pandas", "pyarrow", "matplotlib"} <= present


# --- the PyPI-backed check ------------------------------------------------------------

_MANYLINUX = {"amd64": re.compile(r"(manylinux[0-9_]*|linux)_x86_64"),
              "arm64": re.compile(r"(manylinux[0-9_]*|linux)_aarch64")}


@cache
def pypi_files(name: str, version: str) -> dict[str, str]:
    """{sha256: filename} for one release, from PyPI's JSON API."""
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=20) as response:  # noqa: S310 - fixed https host
        data = json.load(response)
    return {f["digests"]["sha256"]: f["filename"] for f in data["urls"]}


def installs_on(filename: str, arch: str) -> bool:
    if not filename.endswith(".whl"):
        return False
    py, abi, plat = filename[:-4].split("-")[-3:]
    if plat == "any":
        return True
    py_ok = any(t in {"cp311", "py3"} or (t.startswith("cp3") and abi == "abi3"
                                         and int(t[3:]) <= 11) for t in py.split("."))
    return py_ok and any(_MANYLINUX[arch].fullmatch(p) for p in plat.split("."))


@pytest.fixture(scope="module")
def pypi():
    try:
        pypi_files("csvkit", "2.2.0")
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"PyPI unreachable ({exc}); wheel-per-arch check not exercised")
    return pypi_files


def _unusable(pypi, path: Path, arch: str) -> dict:
    locked = parse_lock(path, arch)
    with ThreadPoolExecutor(max_workers=16) as pool:
        files = dict(zip(locked, pool.map(lambda n: pypi(n, locked[n][0]), locked),
                         strict=True))
    unusable = {}
    for name, (version, hashes) in locked.items():
        names = [files[name][h] for h in hashes if h in files[name]]
        if name not in SDIST_OK and not any(installs_on(f, arch) for f in names):
            unusable[f"{name}=={version}"] = names
    return unusable


LOCK_FILES = [DOCKER / "toolbelt.lock", DOCKER / "toolbelt.arm64.lock",
              *(GATEWAY / n for n in (*BASE_LOCKS, *ROLE_LOCKS.values(),
                                      "requirements-snowflake.lock"))]


@pytest.mark.parametrize("arch", ARCHES)
@pytest.mark.parametrize("path", LOCK_FILES, ids=lambda p: p.name)
def test_every_package_has_a_wheel_for_its_arch(pypi, path, arch):
    """`pip install --require-hashes` on arm64 needs an aarch64 manylinux (or pure)
    wheel whose hash is IN the lock - not merely on PyPI. No sdist builds: the image
    has no compiler, so an sdist-only package is a build failure (SDIST_OK aside)."""
    if path.name.endswith(".lock") and ".arm64." in path.name and arch == "amd64":
        pytest.skip("the arm64 venv lock is installed on arm64 only")
    if path.name == "toolbelt.lock" and arch == "arm64":
        pytest.skip("the amd64 venv lock is installed on amd64 only")
    unusable = _unusable(pypi, path, arch)
    assert not unusable, f"{path.name}/{arch}: no installable wheel hashed in the lock: {unusable}"


# --- the warehouse-client lock ---------------------------------------------------------

def test_snowflake_lock_pins_the_connector_and_the_adapter():
    path = GATEWAY / "requirements-snowflake.lock"
    assert "lock_snowflake_clients.py" in header_command(path)
    locked = versions(path)
    assert "snowflake-connector-python" in locked
    assert "dbt-snowflake" in locked


@pytest.mark.parametrize("other", (*BASE_LOCKS, *ROLE_LOCKS.values()))
def test_gateway_locks_agree_on_every_shared_version(other):
    """The snowflake clients are compiled against the base, native and ds pins, so a
    package they share with any of them carries one version - except certifi,
    which dbt-snowflake caps (docker/snowflake-clients.overrides) and which only a
    snowflake-client image takes at the capped version."""
    snow = versions(GATEWAY / "requirements-snowflake.lock")
    base = versions(GATEWAY / other)
    clash = {k: (snow[k], base[k]) for k in set(snow) & set(base) if snow[k] != base[k]}
    assert set(clash) <= {"certifi"}, clash


def test_snowflake_clients_install_after_the_role():
    text = (DOCKER / "Dockerfile").read_text(encoding="utf-8")
    role = text.index("/opt/gateway/requirements-$ALKERA_TOOLBELT.lock")
    snow = text.index("-r /opt/gateway/requirements-snowflake.lock")
    assert role < snow < text.index("--no-deps /opt/gateway")


def test_base_locks_keep_the_pre_snowflake_certifi_and_sqlglot():
    # the reason the clients are compiled apart: nothing they cap reaches these
    for name in BASE_LOCKS:
        locked = versions(GATEWAY / name)
        assert locked.get("certifi") == "2026.7.22", name
        assert locked.get("sqlglot", "30.18.0") == "30.18.0", name


def test_requirements_lock_now_carries_dbt_postgres():
    assert "dbt-postgres" in versions(GATEWAY / "requirements.lock")


# --- the build recipe and the probe know about roles ------------------------------------

def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, DOCKER / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dockerfile_installs_role_libraries_into_the_system_python():
    text = (DOCKER / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG ALKERA_TOOLBELT=de" in text
    assert 'ARG ALKERA_WAREHOUSE_CLIENTS=""' in text
    for lock in (*ROLE_LOCKS.values(), "requirements-snowflake.lock"):
        assert f"COPY gateway/{lock} /opt/gateway/{lock}" in text
    role_install = text.index("/opt/gateway/requirements-$ALKERA_TOOLBELT.lock")
    gateway_install = text.index("--no-deps /opt/gateway")
    assert role_install < gateway_install, "role libraries go in before the gateway"
    # the audited kernelspec is installed system-wide, and checked against the
    # checked-in copy the Jupyter plug's launcher serves from /opt/stack/jupyter
    assert "install_kernelspec('/usr/local/share/jupyter'" in text
    assert "/opt/stack/jupyter/kernels/alkera-python/kernel.json" in text


def test_build_episode_maps_roles_to_tags():
    build = _load("build_episode")
    assert tuple(build.TOOLBELTS) == ROLES
    assert build.image_tag("alkera-episode:head", "de", "") == "alkera-episode:head"
    assert build.image_tag("alkera-episode:head", "ds", "") == "alkera-episode:head-ds"
    assert build.image_tag("alkera-episode:head", "da", "snowflake") == \
        "alkera-episode:head-da-snowflake"
    assert build.build_args("ds", "snowflake") == [
        "--build-arg", "ALKERA_TOOLBELT=ds",
        "--build-arg", "ALKERA_WAREHOUSE_CLIENTS=snowflake",
    ]


def test_toolbelt_check_probes_and_forbids_per_role():
    check = _load("toolbelt_check")
    assert check.ROLE_PYTHON == "/usr/local/bin/python"
    for role in ROLES:
        assert not set(check.role_imports(role)) & set(check.forbidden_imports(role))
    assert {"pandas", "pyarrow", "polars", "sklearn", "matplotlib", "ipykernel",
            "alkera_gateway.native.notebook"} <= set(check.role_imports("ds"))
    assert {"polars", "sklearn"} <= set(check.forbidden_imports("da"))
    assert "alkera_gateway.native.notebook" in check.role_imports("da")
    assert {"pandas", "pyarrow", "ipykernel", "jupyter_server"} <= \
        set(check.forbidden_imports("de"))
    for role in ("ds", "da"):
        argvs = {tuple(argv) for _, argv, _ in check.role_probes(role)}
        assert ("jupyter", "kernelspec", "list") in argvs
        assert ("jupyter", "server", "--version") in argvs
    assert check.KERNEL_NAME == "alkera-python"
    assert not check.role_probes("de")
    assert "jupyter" in check.forbidden_binaries("de")
    assert "jupyter" not in check.forbidden_binaries("da")
    probes = {name: tuple(argv) for name, argv, _ in check.PROBES}
    assert probes["mf"] == ("mf", "--version")
