"""A world that runs MORE THAN ONE warehouse, built by the world factory.

`world/world/build.py` seeds additional warehouses when a seed file opts in
(`warehouses: [legacy, target]`) against a theme that declares them (theme.yaml
`warehouses:`), through `world.warehouse_seed` - bulk, never the connector.
revenue_daily declares two: `legacy`, the decommissioned pre-dbt mart
(read-only, complete), and `target`, where a cutover lands (raw layer only,
writable, a dbt target). Four properties, and the first is the one every
existing world depends on:

1. A seed that names no warehouse builds the world it always built. Not
   "the same hash": the same SECTIONS, and the dual world's fingerprint minus
   its `warehouses` section is the plain world's fingerprint. The second
   warehouse adds one section and moves nothing else.
2. The dual world reproduces: two builds from one seed, one hash, and the
   `warehouses` section covers the new files (a changed cell changes it).
3. The record a task builder publishes to the harness (`build/warehouses.json`
   -> `build.gateway_warehouses`) is deterministic and gateway-shaped.
4. dbt can be pointed at the target warehouse by name - `--target target`,
   from the scheduler's dbt job kind and from the same profiles file the
   gateway's dbt_* tools read - once SUBSTRATE.patch's executor change is in
   (skipped, not failed, until then).
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

import duckdb
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world import build as wbuild, executor, fingerprint, scheduler, snapshot  # noqa: E402
from world.hashing import file_hash  # noqa: E402

TRACK_A = Path(__file__).resolve().parents[1]
SEED = TRACK_A / "seed.example.yaml"
LOCK = TRACK_A / "requirements.lock"
LEGACY = Path("var/warehouse/legacy.duckdb")
TARGET = Path("var/warehouse/target.duckdb")

executor_patch = pytest.mark.skipif(
    not hasattr(executor, "extra_warehouses"),
    reason="the executor half of SUBSTRATE.patch (dbt --target) is not applied",
)


def seed_with(tmp: Path, name: str, warehouses: str) -> Path:
    seed = tmp / name
    seed.write_text(SEED.read_text(encoding="utf-8") + f"warehouses: {warehouses}\n",
                    encoding="utf-8")
    return seed


@pytest.fixture(scope="module")
def worlds():
    tmp = Path(tempfile.mkdtemp(prefix="world_dual_"))
    lock = LOCK if LOCK.exists() else None
    dual_seed = seed_with(tmp, "dual.yaml", "[legacy, target]")
    plain = wbuild.build(SEED, tmp / "plain", lock)
    dual_a = wbuild.build(dual_seed, tmp / "dual_a", lock)
    dual_b = wbuild.build(dual_seed, tmp / "dual_b", lock)
    print("\nplain semantic_hash:", fingerprint.semantic_hash(plain))
    print("dual  semantic_hash:", fingerprint.semantic_hash(dual_a))
    yield {"tmp": tmp, "seed": dual_seed, "plain": plain, "dual_a": dual_a, "dual_b": dual_b}
    shutil.rmtree(tmp, ignore_errors=True)


def fact_rows(db: Path, catalog: str):
    with duckdb.connect(str(db), read_only=True) as conn:
        rows = conn.execute(
            f"SELECT order_date, orders, revenue FROM {catalog}.warehouse.fct_revenue ORDER BY 1"
        ).fetchall()
    return [(d, n, str(r)) for d, n, r in rows]


# ----------------------------------------------------- 1. one warehouse is today


def test_a_seed_that_names_no_warehouse_builds_the_world_it_always_built(worlds):
    plain = fingerprint.semantic(worlds["plain"])
    assert "warehouses" not in plain
    assert not (worlds["plain"] / LEGACY).exists()
    assert not (worlds["plain"] / TARGET).exists()
    assert not (worlds["plain"] / wbuild.WAREHOUSES_RECORD).exists()
    assert wbuild.declared_warehouses(worlds["plain"]) == []
    assert wbuild.gateway_warehouses(worlds["plain"]) == ([], None)


def test_the_second_warehouse_adds_one_section_and_moves_nothing_else(worlds):
    plain = fingerprint.semantic(worlds["plain"])
    dual = fingerprint.semantic(worlds["dual_a"])
    assert sorted(dual) == sorted([*plain, "warehouses"])
    assert {k: v for k, v in dual.items() if k != "warehouses"} == plain


# --------------------------------------------------------- 2. and it reproduces


def test_the_dual_world_reproduces(worlds):
    assert fingerprint.semantic(worlds["dual_a"]) == fingerprint.semantic(worlds["dual_b"])
    for db in (LEGACY, TARGET):
        assert file_hash(worlds["dual_a"] / db) == file_hash(worlds["dual_b"] / db)


def test_the_warehouses_section_covers_the_extra_files(worlds):
    section = fingerprint.semantic(worlds["dual_a"])["warehouses"]
    assert list(section) == ["legacy", "target"]
    assert sorted(section["legacy"]["tables"]) == ["raw.daily_revenue", "warehouse.fct_revenue"]
    assert sorted(section["target"]["tables"]) == ["raw.daily_revenue"]
    moved = shutil.copytree(worlds["dual_a"], worlds["tmp"] / "moved")
    with duckdb.connect(str(moved / LEGACY)) as conn:
        conn.execute("UPDATE legacy.warehouse.fct_revenue SET revenue = revenue + 1 "
                     "WHERE order_date = (SELECT min(order_date) FROM legacy.warehouse.fct_revenue)")
    after = fingerprint.semantic(moved)
    assert after["warehouses"]["legacy"] != section["legacy"]
    assert after["warehouses"]["target"] == section["target"]
    assert after["warehouse"] == fingerprint.semantic(worlds["dual_a"])["warehouse"]


def test_the_legacy_mart_agrees_with_the_dbt_built_primary(worlds):
    """The seeded legacy fact is the same daily numbers dbt derives, which is
    what makes "the two marts agree" a checkable starting state for a cutover."""
    root = worlds["dual_a"]
    legacy = fact_rows(root / LEGACY, "legacy")
    primary = fact_rows(root / "var/warehouse/warehouse.duckdb", "warehouse")
    assert len(legacy) == 30
    assert legacy == primary


def test_snapshot_restore_carries_the_extra_warehouses(worlds):
    tar = snapshot.save(worlds["dual_a"], worlds["tmp"] / "dual.tar")
    restored = snapshot.restore(tar, worlds["tmp"] / "restored")
    assert snapshot.verify(worlds["dual_a"], restored)
    for db in (LEGACY, TARGET):
        assert file_hash(restored / db) == file_hash(worlds["dual_a"] / db)


# ------------------------------------------------- 3. what the builder publishes


def test_the_declaration_record_is_deterministic_and_gateway_shaped(worlds):
    a = (worlds["dual_a"] / wbuild.WAREHOUSES_RECORD).read_bytes()
    b = (worlds["dual_b"] / wbuild.WAREHOUSES_RECORD).read_bytes()
    assert a == b
    assert json.loads(a) == [
        {"attach": True, "backend": "duckdb", "name": "legacy",
         "path": "var/warehouse/legacy.duckdb", "read_only": True,
         "tables": ["raw.daily_revenue", "warehouse.fct_revenue"]},
        {"attach": True, "backend": "duckdb", "dbt_target": "target", "name": "target",
         "path": "var/warehouse/target.duckdb", "read_only": False,
         "tables": ["raw.daily_revenue"]},
    ]
    specs, fp = wbuild.gateway_warehouses(worlds["dual_a"])
    assert fp is None, "a local warehouse needs no builder-side fingerprint"
    assert specs == [
        {"name": "legacy", "backend": "duckdb", "read_only": True, "attach": True,
         "path": str(worlds["dual_a"] / LEGACY)},
        {"name": "target", "backend": "duckdb", "read_only": False, "attach": True,
         "path": str(worlds["dual_a"] / TARGET), "dbt_target": "target"},
    ]
    assert all(Path(spec["path"]).is_absolute() for spec in specs)


def test_a_seed_naming_an_undeclared_warehouse_fails_at_build(worlds):
    with pytest.raises(SystemExit, match="does not declare"):
        wbuild.build(seed_with(worlds["tmp"], "bad.yaml", "[nonesuch]"), worlds["tmp"] / "bad")
    with pytest.raises(SystemExit, match="not usable"):
        wbuild.build(seed_with(worlds["tmp"], "bad2.yaml", "[warehouse]"), worlds["tmp"] / "bad2")


def test_a_read_only_warehouse_cannot_be_a_dbt_target(worlds, monkeypatch):
    """dbt opens the file itself and would write past the engine's read-only
    rule; a theme that declares both is refused at build, naming the pair."""
    from world.themes import load_theme

    theme = load_theme("revenue_daily")
    manifest = dict(theme.manifest)
    manifest["warehouses"] = {"legacy": {**theme.manifest["warehouses"]["legacy"], "dbt": True}}
    monkeypatch.setattr(wbuild, "load_theme", lambda name: type(theme)(
        name=theme.name, dir=theme.dir, manifest=manifest, gen=theme.gen))
    with pytest.raises(SystemExit, match="read-only and cannot also be a dbt target"):
        wbuild.build(seed_with(worlds["tmp"], "ro.yaml", "[legacy]"), worlds["tmp"] / "ro")


# ------------------------------------------------------- 4. dbt reaches it too


@executor_patch
def test_profiles_carry_one_output_per_duckdb_file(worlds):
    profiles = yaml.safe_load(
        (worlds["dual_a"] / "build" / "profiles.yml").read_text(encoding="utf-8"))
    outputs = profiles["world_dbt"]["outputs"]
    assert profiles["world_dbt"]["target"] == "dev"
    assert set(outputs) == {"dev", "legacy", "target"}
    assert outputs["target"] == {"type": "duckdb", "threads": 1,
                                 "path": str(worlds["dual_a"] / TARGET)}
    plain = yaml.safe_load(
        (worlds["plain"] / "build" / "profiles.yml").read_text(encoding="utf-8"))
    assert set(plain["world_dbt"]["outputs"]) == {"dev"}


@executor_patch
def test_dbt_builds_the_target_warehouse_by_name(worlds):
    """`dbt build --target target` builds the marts from the TARGET's raw
    layer, inside its own file; the primary and the legacy files do not move,
    and the fact it builds agrees with the legacy mart it is replacing."""
    root = shutil.copytree(worlds["dual_a"], worlds["tmp"] / "dbt_target")
    before = fingerprint.semantic(root)
    rc, output = executor.dbt_invoke(root, "build", target="target")
    assert rc == 0, output
    after = fingerprint.semantic(root)
    assert after["warehouse"] == before["warehouse"]
    assert after["warehouses"]["legacy"] == before["warehouses"]["legacy"]
    assert after["warehouses"]["target"] != before["warehouses"]["target"]
    with duckdb.connect(str(root / TARGET), read_only=True) as conn:
        assert conn.execute(
            "select count(*) from duckdb_views() where not internal and view_name = 'stg_revenue'"
        ).fetchone()[0] == 1
    assert fact_rows(root / TARGET, "target") == fact_rows(root / LEGACY, "legacy")
    assert executor.unreadable_views(root) == {}


@executor_patch
def test_a_scheduled_dbt_job_can_name_the_target(worlds):
    root = shutil.copytree(worlds["dual_a"], worlds["tmp"] / "sched_target")
    result = executor.execute(
        root, "target_build",
        {"kind": "dbt", "command": "build", "target": "target",
         "tables": ["warehouse.fct_revenue"]})
    assert result["ok"], result
    assert result["detail"] == "dbt build --target target"
    missing = executor.execute(root, "nope", {"kind": "dbt", "command": "build", "target": "nope"})
    assert not missing["ok"] and "nope" in (missing["error"] or "")


@executor_patch
def test_advance_still_reproduces_with_extra_warehouses(worlds):
    """The scheduler re-renders profiles.yml on every dbt job; with three
    outputs that must still be a pure function of the tree."""
    a = shutil.copytree(worlds["dual_a"], worlds["tmp"] / "adv_a")
    b = shutil.copytree(worlds["dual_b"], worlds["tmp"] / "adv_b")
    scheduler.advance(a, 30)
    scheduler.advance(b, 30)
    assert fingerprint.semantic(a) == fingerprint.semantic(b)
    assert fingerprint.semantic(a)["warehouses"] == fingerprint.semantic(worlds["dual_a"])["warehouses"]
