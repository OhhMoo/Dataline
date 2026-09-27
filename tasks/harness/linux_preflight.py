"""Can this Linux host run a measurement-valid native+container rollout?

    gateway/.venv/bin/python tasks/harness/linux_preflight.py              # checks only
    gateway/.venv/bin/python tasks/harness/linux_preflight.py --live       # + a real stack, no model
    gateway/.venv/bin/python tasks/harness/linux_preflight.py --live --model-smoke opus

Every check is one the harness would otherwise fail on half way through an
episode, after a world was built and quota spent. `--live` hands off to
`test_subject_sandbox.py`, which brings up GT001's stack with the hard sandbox
(README.md#the-subject-sandbox), proves the subject's shell lands in the episode, and tries every
escape from the subject container. `--model-smoke` spends ONE model turn doing
the same through the real CLI in that container; it is the acceptance for a
new host.

Exit 0 when nothing FAILED. WARN lines do not block, and say what they cost.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import container_episode as ce  # noqa: E402
import episode  # noqa: E402

#: `!override` in compose.episode.yml and compose.native.yml.
MIN_COMPOSE = (2, 24)
MIN_FREE_GB = 20

RESULTS: list[tuple[str, str, str]] = []


def report(status: str, name: str, detail: str = "") -> None:
    RESULTS.append((status, name, detail))
    print(f"  [{status:4}] {name}" + (f" - {detail}" if detail else ""))


def run(argv: list[str], timeout: int = 60) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def check_platform() -> None:
    if sys.platform.startswith("linux"):
        report("OK", "platform", f"{sys.platform}, python {sys.version.split()[0]}")
    else:
        report("FAIL", "platform", f"{sys.platform}: this preflight is for Linux hosts")


def check_engine() -> None:
    if not shutil.which("docker"):
        report("FAIL", "docker CLI", "not on PATH; run scripts/setup_linux.sh")
        return
    ok, detail = ce.docker_available()
    report("OK" if ok else "FAIL", "container engine reachable", detail[:200])
    if not ok:
        return
    r = run(["docker", "compose", "version", "--short"])
    text = (r.stdout.strip() if r else "") or ""
    m = re.search(r"(\d+)\.(\d+)", text)
    if not m:
        report("FAIL", "docker compose", f"no compose v2 plugin ({text or 'absent'})")
    elif (int(m[1]), int(m[2])) < MIN_COMPOSE:
        report("FAIL", "docker compose",
               f"{text} < {MIN_COMPOSE[0]}.{MIN_COMPOSE[1]} (compose files use !override)")
    else:
        report("OK", "docker compose", text)
    info = run(["docker", "info", "--format", "{{json .}}"])
    blob = (info.stdout if info else "").lower()
    rootless = "rootless" in blob
    podman = "podman" in blob or "podman" in (os.environ.get("DOCKER_HOST") or "")
    engine = ("podman" if podman else "docker") + (" (rootless)" if rootless else " (rootful)")
    if rootless:
        report("OK", "engine", f"{engine}: files the agent writes are owned by you")
    else:
        report("OK", "engine", f"{engine}: container writes are handed back to "
               f"uid {os.getuid()} at teardown (container_episode.release_ownership)")
    if ce.selinux_enforcing():
        mode = os.environ.get("ALKERA_SELINUX_LABEL") == "keep"
        report("WARN" if mode else "OK", "SELinux enforcing",
               "ALKERA_SELINUX_LABEL=keep: you must relabel rollout dirs yourself" if mode
               else "each project gets a label=disable overlay (container_episode.selinux_overlay)")


def check_image(image: str) -> None:
    labels = ce._inspect_image_labels(image)
    if labels is None:
        report("FAIL", "episode image", f"{image} not built; run "
               f"gateway/.venv/bin/python gateway/docker/build_episode.py")
        return
    try:
        state = ce.assert_image_current(image)
        report("OK", "episode image", f"{image} @ {str(state.get('revision'))[:12]}")
    except Exception as exc:                               # noqa: BLE001 - reported
        report("FAIL", "episode image", str(exc).splitlines()[0][:300])


def check_subject_image(image: str) -> None:
    try:
        state = ce.subject_image_state(image)
        report("OK", "subject image (hard sandbox)",
               f"{image}, claude {state['claude_version']}")
    except Exception as exc:                               # noqa: BLE001 - reported
        report("FAIL", "subject image (hard sandbox)", str(exc)[:300])


def check_tree() -> None:
    r = run(["git", "-C", str(ce.REPO), "status", "--porcelain"])
    dirty = [line for line in (r.stdout if r else "").splitlines() if line.strip()]
    if dirty:
        report("WARN", "git tree", f"{len(dirty)} uncommitted paths; rollouts record "
               f"HEAD, so a result will not point at the code that produced it")
    else:
        report("OK", "git tree", "clean")


def check_subject() -> None:
    claude = shutil.which("claude")
    if not claude:
        report("FAIL", "claude CLI", "not on PATH; curl -fsSL https://claude.ai/install.sh | bash")
    else:
        r = run([claude, "--version"])
        report("OK", "claude CLI", (r.stdout.strip() if r else "?")[:80])
    try:
        how = ce.require_subject_auth()
        report("OK", "subject credential", f"${how}")
    except ce.ContainerError as exc:
        report("FAIL", "subject credential", str(exc)[:300])
    inherited = [v for v in ce.PARENT_SESSION_VARS if v in os.environ]
    if inherited:
        report("WARN", "parent session", f"started inside a Claude Code session "
               f"({', '.join(inherited[:3])}...); stripped from every subject, but "
               f"prefer launching from a plain terminal or tmux")
    bash = shutil.which("bash")
    report("OK" if bash else "FAIL", "host bash", bash or "the redirect needs bash")


def check_venv() -> None:
    py = Path(episode.PY)
    if not py.exists():
        report("FAIL", "gateway venv", f"{py} missing; run scripts/setup_linux.sh")
        return
    r = run([str(py), "-c", "import alkera_gateway, duckdb, fastapi, dbt; print('ok')"], 120)
    good = bool(r) and r.returncode == 0
    report("OK" if good else "FAIL", "gateway venv",
           str(py) if good else ((r.stderr if r else "")[-300:]))


def check_disk() -> None:
    import tempfile
    free = shutil.disk_usage(tempfile.gettempdir()).free / 2**30
    report("OK" if free >= MIN_FREE_GB else "WARN", "scratch disk",
           f"{free:.0f} GB free under {tempfile.gettempdir()} (rollouts land there)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", default=ce.DEFAULT_IMAGE)
    parser.add_argument("--subject-image", default=ce.SUBJECT_IMAGE)
    parser.add_argument("--live", action="store_true",
                        help="bring up the GT001 stack with the hard sandbox, "
                             "prove it, and try the escapes")
    parser.add_argument("--model-smoke", default=None, metavar="MODEL",
                        help="with --live: one model turn through the real CLI")
    args = parser.parse_args(argv)

    print("alkera linux preflight")
    check_platform()
    check_engine()
    check_image(args.image)
    check_subject_image(args.subject_image)
    check_tree()
    check_venv()
    check_subject()
    check_disk()
    failed = [r for r in RESULTS if r[0] == "FAIL"]
    print(f"\n  {len(RESULTS) - len(failed)} ok/warn, {len(failed)} failed")
    if failed or not args.live:
        return 1 if failed else 0

    print("\n--live: test_subject_sandbox.py (stack, proof, escapes)")
    live = [sys.executable, str(HERE / "test_subject_sandbox.py"), "--image", args.image,
            "--subject-image", args.subject_image]
    if args.model_smoke:
        live += ["--model-smoke", args.model_smoke]
    return subprocess.call(live, env=ce.subject_base_env())


if __name__ == "__main__":
    raise SystemExit(main())
