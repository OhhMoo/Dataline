#!/usr/bin/env python3
"""A real `alkera_gateway.Gateway` over a GT009 world. Task-agnostic.

    from probes import gateway
    gw = gateway.open_gateway(world, audit_dir, repo_root=REPO)

One warehouse, so every key comes straight out of the manifest
`tests/build_world.py` prints - there is no key here the rollout harness does
not already carry.

The one thing this adds over a bare `Gateway(config)` is the scheduler:
`scheduling.episode_scheduler(config)` is the WorldScheduler, injected exactly
as `alkera_gateway.cli serve` injects it, so `sched_trigger` executes a job's
real script instead of reporting `not_executed`. Without it every baseline in
this package would be measuring a scheduler that does nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path


def open_gateway(world: dict, audit_dir, *, repo_root, episode_id: str = "probe"):
    for extra in (Path(repo_root) / "gateway" / "src", Path(repo_root) / "world"):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    from alkera_gateway.config import GatewayConfig          # noqa: PLC0415
    from alkera_gateway.core import Gateway                  # noqa: PLC0415
    from alkera_gateway.scheduling import episode_scheduler  # noqa: PLC0415
    observability = Path(world["observability"])
    config = GatewayConfig(
        workspace=Path(world["workspace"]),
        warehouse=Path(world["warehouse"]),
        scheduler=Path(world["scheduler"]),
        dashboards=Path(world["dashboards"]),
        observability=observability,
        obs_samples=Path(world.get("obs_samples")
                         or observability.parent / "samples.jsonl"),
        workflow_dir=Path(world["workflow_dir"]),
        audit_dir=Path(audit_dir),
        profiles_dir=Path(world["profiles_dir"]),
        world_root=Path(world["world_root"]),
        episode_id=episode_id,
        virtual_time=world["virtual_time"],
    )
    return Gateway(config, scheduler=episode_scheduler(config))


def call(gw, tool: str, args: dict) -> dict:
    """One tool call; raises with the gateway's own error on failure."""
    answer = gw.call(tool, args)
    if not answer["ok"]:
        raise RuntimeError(f"{tool} {args!r} failed: {answer['error']}")
    return answer["result"]
