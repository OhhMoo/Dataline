"""`./ops/deploy` - the release CLI the workspace puts on the agent's PATH.

It is an HTTP client for `deploy.py` and nothing else: no module import, no
fixture read, no second code path. Everything it does is therefore audited by
the service, which is the whole point of native mode - a CLI that touched the
world directly would leave no 9.2.2 row.

    ./ops/deploy list
    ./ops/deploy run <name> [--full-refresh] [--var KEY=VALUE ...]
    ./ops/deploy rollback <name>

Exit codes, because a release tool is read by scripts as often as by people:

    0  the deploy ran and its canary is clean
    1  the deploy's own command failed (or `rollback`, which reverts nothing)
    2  the deploy ran and its canary is breached or could not be measured
    3  the request was refused (unknown deploy, service down)

`rollback` is deliberately not a revert. The world's deploy surface has no
inverse operation and inventing one - re-running the deploy, restoring from a
snapshot the agent cannot see - would be a lie in the most expensive possible
place. It records an `inactive` deployment status, prints what was not done, and
exits 1.
"""
from __future__ import annotations

import argparse
import os
import sys

import httpx

# Spelled out rather than imported from `deploy.py`: the CLI is an HTTP client
# and must not need the service's dependencies to run.
DEFAULT_URL = "http://deploy.internal:8085"
DEFAULT_REPO = "alkera/warehouse"
TIMEOUT_S = 300


def endpoint() -> tuple[str, str]:
    url = os.environ.get("ALKERA_DEPLOY_URL", DEFAULT_URL).rstrip("/")
    repo = os.environ.get("ALKERA_DEPLOY_REPO", DEFAULT_REPO).strip("/")
    return url, repo


def request(method: str, path: str, payload: dict | None = None) -> object:
    url, repo = endpoint()
    try:
        response = httpx.request(
            method, f"{url}/repos/{repo}{path}", json=payload, timeout=TIMEOUT_S
        )
    except httpx.HTTPError as exc:
        print(f"deploy: cannot reach {url}: {exc}", file=sys.stderr)
        raise SystemExit(3) from None
    if response.status_code >= 400:
        detail = response.json().get("message", response.text) if response.content else ""
        print(f"deploy: {response.status_code} {detail}", file=sys.stderr)
        raise SystemExit(3)
    return response.json()


def command_list() -> int:
    for environment in request("GET", "/environments")["environments"]:
        spec = environment["alkera_deploy"]
        canary = spec["canary"]
        print(f"{environment['name']}  [{spec['kind']}]  {environment['description'] or ''}")
        print(f"    metrics: {', '.join(canary.get('metrics') or []) or '-'}")
        print(f"    alarms:  {', '.join(canary.get('alarms') or []) or '-'}")
        print(f"    guards:  {len(canary.get('guards') or [])}")
    return 0


def command_run(name: str, full_refresh: bool, variables: dict) -> int:
    body = {
        "ref": name,
        "environment": name,
        "payload": {"deploy": name, "full_refresh": full_refresh, "variables": variables},
    }
    deployment = request("POST", "/deployments", body)
    canary = deployment["alkera_canary"]
    print(f"deployment {deployment['id']}  {name}  canary {canary['status']}")
    if canary["newly_firing"]:
        print(f"    newly firing: {', '.join(canary['newly_firing'])}")
    if canary["cleared"]:
        print(f"    cleared:      {', '.join(canary['cleared'])}")
    print(f"    status:       {deployment['alkera_status']['state']}")
    if canary["returncode"]:
        print(f"deploy: command exited {canary['returncode']}", file=sys.stderr)
        return 1
    # Reported, never blocked - the operator decides what a breach means.
    return 0 if canary["status"] == "clean" else 2


def command_rollback(name: str) -> int:
    deployments = request("GET", f"/deployments?environment={name}")
    if not deployments:
        print(f"deploy: {name} has never been deployed", file=sys.stderr)
        return 3
    latest = deployments[-1]
    request(
        "POST",
        f"/deployments/{latest['id']}/statuses",
        {"state": "inactive", "description": "rollback requested from ./ops/deploy"},
    )
    print(f"deployment {latest['id']} ({name}) marked inactive.")
    print("NOTHING WAS REVERTED: this release surface has no inverse operation.")
    print("Undo the change yourself - re-run the deploy with fixed inputs, or")
    print("write the compensating SQL - then deploy again so the canary measures it.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deploy", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="the deploys this world declares, and their canaries")
    run = commands.add_parser("run", help="run a deploy and report its canary")
    run.add_argument("name")
    run.add_argument("--full-refresh", action="store_true",
                     help="rebuild from scratch; classified destructive")
    run.add_argument("--var", action="append", default=[], metavar="KEY=VALUE")
    rollback = commands.add_parser("rollback", help="record that a deploy should be undone")
    rollback.add_argument("name")
    args = parser.parse_args(argv)
    if args.command == "list":
        return command_list()
    if args.command == "rollback":
        return command_rollback(args.name)
    variables = {}
    for pair in args.var:
        if "=" not in pair:
            parser.error(f"--var wants KEY=VALUE, got {pair!r}")
        key, value = pair.split("=", 1)
        variables[key] = value
    return command_run(args.name, args.full_refresh, variables)


if __name__ == "__main__":
    raise SystemExit(main())
