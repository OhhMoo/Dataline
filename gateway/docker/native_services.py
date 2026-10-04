"""The `native-services` container's entrypoint (compose.native.yml).

    python /opt/stack/native_services.py --state-dir /service-state -- \\
        --audit /audit --world /world --bind 0.0.0.0 [--profile ... --plug ...]

Everything after `--` is `alkera_gateway.native.serve`'s own argv, passed
through unchanged. Before it execs serve, this writes the services' gateway
config into `--state-dir` with `bi_state_dir` under it, and hands serve that
file with `--gateway-config`.

WHY. serve.py, given no `--gateway-config`, derives one
(`serve.world_gateway_config`) and writes it to `<audit>/native_gateway.json`,
and `GatewayConfig.bi_state_dir` then defaults to `<audit>/bi`. In this
topology `/audit` is ALSO mounted read-write into the episode container - the
PATH shims run there and append their 9.2.2 rows - so both were the subject's
to write: a "published" Sigma workbook or Tableau data source dropped into
`/audit/bi/...` was what `tasks/harness/bistate.py` staged for the grader, and
because the services re-read their config on every request
(`native/context.process_context`), an edited `/audit/native_gateway.json`
re-pointed every service at whatever the subject liked. `--state-dir` is a
mount this container has and the episode, jupyter and subject containers do
not (compose.native.yml; containment P7 asserts it per rollout), so the BI
state and the config the services read are now writable by this process only.

The config is serve.py's own derivation, byte for byte, with one key changed:
the same `--world` / `--audit` layout, the same `audit_dir` (the action log
and destinations.jsonl stay in `/audit`, where the harness and the shims need
them), and `bi_state_dir = <state-dir>/bi`. It lives in gateway/docker rather
than in serve.py because that directory is what the episode image takes from
the working tree (build_episode.py's overlay) - the launcher precedent is
start.py and jupyter/launch.py. Standard library plus the gateway the image
already carries; it runs in native-services only (it is under /opt/stack,
which is stripped from the episode container before the agent starts).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

#: The config file the services read, inside `--state-dir`.
CONFIG_NAME = "native_gateway.json"
#: The BI vendors' published state, inside `--state-dir`
#: (`tasks/harness/bistate.BI_SUBDIR`).
BI_SUBDIR = "bi"
SERVE_MODULE = "alkera_gateway.native.serve"


def services_config(world: Path, audit: Path, state_dir: Path) -> dict:
    """serve.py's derived config for this world, with the BI state moved into
    `state_dir`. Validated, so a layout GatewayConfig refuses fails here, before
    a socket opens, rather than on the first request."""
    from alkera_gateway.config import GatewayConfig
    from alkera_gateway.native.serve import world_gateway_config

    config = json.loads(world_gateway_config(world, audit))
    config["bi_state_dir"] = str(state_dir / BI_SUBDIR)
    GatewayConfig.model_validate(config)
    return config


def split_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    """(our flags, serve's argv) around the first `--`."""
    if "--" not in argv:
        raise SystemExit("native_services.py: serve's arguments go after `--`")
    cut = argv.index("--")
    return argv[:cut], argv[cut + 1:]


def main(argv: list[str] | None = None) -> int:
    ours, serve_argv = split_argv(list(sys.argv[1:] if argv is None else argv))
    parser = argparse.ArgumentParser(prog="native_services.py")
    parser.add_argument("--state-dir", type=Path, required=True,
                        help="the services' own state: a mount no other "
                             "container in the stack has")
    parser.add_argument("--print-only", action="store_true",
                        help="write the config, print serve's argv, do not exec")
    args = parser.parse_args(ours)

    served = argparse.ArgumentParser(add_help=False)
    served.add_argument("--audit", type=Path, required=True)
    served.add_argument("--world", type=Path, required=True)
    served.add_argument("--gateway-config", default=None)
    known, _ = served.parse_known_args(serve_argv)
    if known.gateway_config:
        raise SystemExit("native_services.py writes the services' config itself; "
                         "a second --gateway-config would put it back where the "
                         "caller chose")
    state_dir = args.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    config_path = state_dir / CONFIG_NAME
    config = services_config(known.world, known.audit, state_dir)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8",
                           newline="")
    command = [sys.executable, "-m", SERVE_MODULE, *serve_argv,
               "--gateway-config", str(config_path)]
    print(f"native-services: state {state_dir} (bi_state_dir "
          f"{config['bi_state_dir']}), config {config_path}", flush=True)
    if args.print_only:
        print(json.dumps(command), flush=True)
        return 0
    os.execv(sys.executable, command)
    return 1  # pragma: no cover - execv does not return


if __name__ == "__main__":
    sys.exit(main())
