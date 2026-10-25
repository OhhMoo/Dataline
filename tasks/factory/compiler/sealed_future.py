"""Keep held-out factory arrivals in the grading plane until shadow replay."""
from __future__ import annotations

import shutil
from pathlib import Path


def seal_future(world: Path, private: Path) -> None:
    """Move generated future arrivals and build provenance out of the subject world."""
    world, private = Path(world), Path(private)
    arrivals = world / "build/factory_future"
    if not arrivals.is_dir():
        raise FileNotFoundError(f"held-out arrivals absent: {arrivals}")
    private.mkdir(parents=True, exist_ok=True)
    target = private / "factory_future"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(arrivals, target)
    shutil.rmtree(arrivals)
    provenance = world / "build/provenance.json"
    if provenance.is_file():
        shutil.copy2(provenance, private / "build_provenance.json")
        provenance.unlink()
