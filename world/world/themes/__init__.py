"""World themes: the world *shape* as data, not code.

A theme is a directory world/themes/<name>/ holding:
  theme.yaml   manifest: name, dbt_profile, scheduler job specs
  gen.py       hooks: write_landing(workspace, seed) -> None
                      load_raw(workspace, db_path) -> None
  workspace/   static files copied verbatim into the built workspace/
  bi/*.yml     dashboard specs copied verbatim to var/bi/dashboards/
  seed.example.yaml

Themes are loaded in place via importlib — they are content, not packages —
so adding a world never touches the builder.
"""
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import yaml


@dataclass
class Theme:
    name: str
    dir: Path
    manifest: dict
    gen: ModuleType


def load_theme(name: str) -> Theme:
    d = Path(__file__).resolve().parent / name
    manifest_path = d / "theme.yaml"
    if not manifest_path.is_file():
        raise SystemExit(f"unknown theme: {name} (no {manifest_path})")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location(
        f"world_theme_{name}_gen", d / "gen.py")
    gen = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gen
    spec.loader.exec_module(gen)
    return Theme(name=manifest["name"], dir=d, manifest=manifest, gen=gen)
