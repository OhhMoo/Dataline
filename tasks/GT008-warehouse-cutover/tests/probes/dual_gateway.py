#!/usr/bin/env python3
"""
A real `alkera_gateway.Gateway` over a world that declares MORE THAN ONE
warehouse, with the world's executing scheduler injected. Task-agnostic.

    from probes import dual_gateway
    gw = dual_gateway.open_gateway(world, audit_dir, repo_root=REPO)

Two things `deploy_probe.open_gateway` does not do, and both decide whether a
cutover task can be exercised at all:

  * it reads `warehouse_name` and `warehouses` out of the world manifest and
    passes them into `GatewayConfig`, so `sql_query`/`sql_execute` grow the
    `warehouse` field and the primary carries its peers as attached catalogs
    (gateway/SURFACES.md#dual-warehouse). Without them the consumer view over
    the legacy catalog cannot resolve and `bi_render` is dead;
  * it injects `scheduling.episode_scheduler(config)` - the WorldScheduler -
    exactly as `alkera_gateway.cli serve` does, so `sched_trigger` executes the
    job's real script instead of reporting `not_executed`.

`tasks/harness/episode.py:write_gateway_config` does neither today; DESIGN.md
carries the patch. This helper is how a baseline suite runs the world before
that patch lands, through the same Gateway class a rollout uses.
"""
from __future__ import annotations

import sys
from pathlib import Path


def open_gateway(world: dict, audit_dir, *, repo_root, episode_id: str = "probe"):
    for extra in (Path(repo_root) / "gateway" / "src", Path(repo_root) / "world"):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    from alkera_gateway.config import GatewayConfig      # noqa: PLC0415
    from alkera_gateway.core import Gateway              # noqa: PLC0415
    from alkera_gateway.scheduling import episode_scheduler  # noqa: PLC0415
    observability = Path(world["observability"])
    config = GatewayConfig(
        workspace=Path(world["workspace"]),
        warehouse=Path(world["warehouse"]),
        scheduler=Path(world["scheduler"]),
        dashboards=Path(world["dashboards"]),
        observability=observability,
        obs_samples=Path(world.get("obs_samples") or observability.parent / "samples.jsonl"),
        workflow_dir=Path(world["workflow_dir"]),
        audit_dir=Path(audit_dir),
        profiles_dir=Path(world["profiles_dir"]),
        world_root=Path(world["world_root"]),
        episode_id=episode_id,
        virtual_time=world["virtual_time"],
        warehouse_name=world.get("warehouse_name", "primary"),
        warehouses=list(world.get("warehouses") or []),
    )
    return Gateway(config, scheduler=episode_scheduler(config))


def call(gateway, tool: str, args: dict) -> dict:
    """One tool call; raises with the gateway's own error on failure."""
    answer = gateway.call(tool, args)
    if not answer["ok"]:
        raise RuntimeError(f"{tool} {args!r} failed: {answer['error']}")
    return answer["result"]
