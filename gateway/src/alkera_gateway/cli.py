import argparse
import asyncio
import json
import logging
from pathlib import Path
from urllib.request import Request, urlopen

from .audit import read_rows, validate_trace
from .config import GatewayConfig
from .core import Gateway
from .records import canonical
from .replay import replay
from .signals import process_signals


def parse_pairs(values: list[str]) -> dict:
    result = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Expected NAME=VALUE, got: {value}")
        name, raw = value.split("=", 1)
        if not name:
            raise ValueError("Argument name cannot be empty")
        try:
            result[name] = json.loads(raw)
        except json.JSONDecodeError:
            result[name] = raw
    return result


def call_remote(url: str, tool: str, args: dict, timeout: int = 180) -> dict:
    request = Request(
        url.rstrip("/") + "/call",
        data=canonical({"tool": tool, "args": args}),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="alkera-gateway")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--config", type=Path, required=True)
    serve.add_argument("--transport", choices=["http", "stdio"], default="http")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    call = commands.add_parser("call")
    call.add_argument("tool")
    source = call.add_mutually_exclusive_group()
    source.add_argument("--args")
    source.add_argument("--args-file", type=Path)
    source.add_argument("--arg", action="append", default=[])
    call.add_argument("--url", default="http://127.0.0.1:8080")
    call.add_argument("--timeout", type=int, default=180)
    trace = commands.add_parser("trace")
    trace.add_argument("audit_dir", type=Path)
    replay_parser = commands.add_parser("replay")
    replay_parser.add_argument("audit_dir", type=Path)
    replay_parser.add_argument("--config", type=Path, required=True)
    signals = commands.add_parser("signals")
    signals.add_argument("audit_dir", type=Path)
    signals.add_argument("--experimental", action="store_true", required=True)
    plugs_parser = commands.add_parser(
        "plugs", help="environment profiles: matrix | show PROFILE [--plug SLOT=PLUG]")
    plugs_parser.add_argument("plugs_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == "plugs":
        from .plugs import main as plugs_main

        return plugs_main(args.plugs_args)
    logging.basicConfig(level=logging.INFO)
    if args.command == "serve":
        from .scheduling import episode_scheduler
        from .server import http_app, serve_stdio

        cfg = GatewayConfig.load(args.config)
        # An episode gets the EXECUTING scheduler; see scheduling.py. Serving a
        # world with a scheduler the agent cannot drive is what let a rollout
        # call sched_trigger five times and be told success every time.
        gateway = Gateway(cfg, scheduler=episode_scheduler(cfg))
        try:
            if args.transport == "stdio":
                asyncio.run(serve_stdio(gateway))
            else:
                import uvicorn

                uvicorn.run(http_app(gateway), host=args.host, port=args.port, workers=1)
        finally:
            gateway.close()
        return 0
    if args.command == "call":
        if args.args_file:
            values = json.loads(args.args_file.read_text(encoding="utf-8"))
        elif args.args:
            values = json.loads(args.args)
        else:
            values = parse_pairs(args.arg)
        result = call_remote(args.url, args.tool, values, args.timeout)
        print(canonical(result).decode("utf-8"))
        return 0 if result["ok"] else 1
    rows = validate_trace(args.audit_dir / "actions.jsonl")
    if args.command == "trace":
        for row in rows:
            print(canonical(row).decode("utf-8"))
        return 0
    if args.command == "signals":
        result = process_signals(rows, read_rows(args.audit_dir / "checkpoints.jsonl"))
    else:
        gateway = Gateway(GatewayConfig.load(args.config))
        try:
            result = replay(args.audit_dir, gateway)
        finally:
            gateway.close()
    print(canonical(result).decode("utf-8"))
    return 0 if result.get("responses_match", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
