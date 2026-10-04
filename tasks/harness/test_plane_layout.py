"""`episode._plane_paths`: a plane the builder seeded but did not publish.

GT002's builder seeds `var/workflow` on its decision arm and publishes no
`workflow_dir`, so the host config named no issue tracker and Jira answered
"This world has no issue tracker" while the container topology (which derives
the 9.2.1 layout) served it. A plane that exists on disk is now filled in from
the layout; a plane that does not exist is still reported absent.
"""
from pathlib import Path

import episode


def test_seeded_but_unpublished_plane_is_filled_from_the_layout(tmp_path: Path):
    (tmp_path / "var" / "workflow").mkdir(parents=True)
    paths = episode._plane_paths({"world_root": str(tmp_path)})
    assert paths == {"workflow_dir": str(tmp_path / "var" / "workflow")}


def test_absent_plane_stays_absent(tmp_path: Path):
    assert episode._plane_paths({"world_root": str(tmp_path)}) == {}


def test_published_key_wins_over_the_layout(tmp_path: Path):
    (tmp_path / "var" / "workflow").mkdir(parents=True)
    elsewhere = str(tmp_path / "planes" / "workflow")
    paths = episode._plane_paths({"world_root": str(tmp_path), "workflow_dir": elsewhere})
    assert paths["workflow_dir"] == elsewhere


def test_written_config_carries_the_filled_in_plane(tmp_path: Path):
    root = tmp_path / "world"
    (root / "var" / "workflow").mkdir(parents=True)
    (root / "var" / "obs").mkdir(parents=True)
    (root / "var" / "obs" / "observability.yml").write_text("{}\n", encoding="utf-8")
    world = {"world_root": str(root), "workspace": str(root / "workspace"),
             "warehouse": str(root / "var/warehouse/warehouse.duckdb"),
             "scheduler": str(root / "var/sched/state.json"),
             "dashboards": str(root / "var/bi/dashboards"),
             "profiles_dir": str(root / "build")}
    import json
    cfg = json.loads(episode.write_gateway_config(
        tmp_path / "gateway.json", world, tmp_path / "audit", "x",
        "2026-03-13T09:00:00Z").read_text(encoding="utf-8"))
    assert cfg["workflow_dir"] == str(root / "var" / "workflow")
    assert cfg["observability"] == str(root / "var" / "obs" / "observability.yml")
    assert "ingest" not in cfg
