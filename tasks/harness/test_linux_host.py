"""The Linux-host helpers in container_episode, without a daemon or a model.

    python tasks/harness/test_linux_host.py

The live half of each (a real engine, a real CLI) is `linux_preflight.py --live
--model-smoke <model>`; this file pins the logic those runs depend on.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import container_episode as ce  # noqa: E402

PASSED = FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name} {detail}")


def test_parent_session_stripped() -> None:
    fake = {"CLAUDECODE": "1", "CLAUDE_CODE_MESSAGING_SOCKET": "/x", "PATH": "/bin",
            "CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDE_PROJECT_DIR": "/repo"}
    with mock.patch.dict(os.environ, fake, clear=True):
        env = ce.subject_base_env({"HOME": "/r"})
    check("parent session vars removed",
          not {"CLAUDECODE", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_PROJECT_DIR"} & set(env))
    check("credential and PATH kept", env.get("CLAUDE_CODE_OAUTH_TOKEN") == "t"
          and env.get("PATH") == "/bin")
    check("extra applied last", env["HOME"] == "/r" and env["PYTHONUTF8"] == "1")


def test_auth() -> None:
    check("token accepted", ce.require_subject_auth({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"})
          == "CLAUDE_CODE_OAUTH_TOKEN")
    check("api key accepted", ce.require_subject_auth({"ANTHROPIC_API_KEY": "sk-ant-api03-x"})
          == "ANTHROPIC_API_KEY")
    with mock.patch.object(ce.sys, "platform", "linux"):
        try:
            ce.require_subject_auth({"CLAUDE_CODE_OAUTH_TOKEN": "  "})
            check("linux without a credential refused", False)
        except ce.ContainerError as exc:
            check("linux without a credential refused", "setup-token" in str(exc))
    with mock.patch.object(ce.sys, "platform", "win32"):
        check("windows falls back to its home", ce.require_subject_auth({}) == "home")
    for bad in ("PASTE_TOKEN_HERE", "sk-ant-oat01-abc def"):
        try:
            ce.require_subject_auth({"CLAUDE_CODE_OAUTH_TOKEN": bad})
            check(f"placeholder / broken paste refused ({bad[:12]})", False)
        except ce.ContainerError as exc:
            check(f"placeholder / broken paste refused ({bad[:12]})",
                  "does not look like" in str(exc) and bad not in str(exc))


def test_selinux_overlay(tmp: Path) -> None:
    fake = tmp / "compose"
    fake.write_text("#!/bin/sh\nprintf 'episode\\npostgres\\nnative-services\\n'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    dest = tmp / "c" / "compose.selinux.yml"
    with mock.patch.object(ce, "selinux_enforcing", return_value=False):
        check("no overlay when SELinux is off", ce.selinux_overlay([str(fake)], {}, dest) is None)
    with mock.patch.object(ce, "selinux_enforcing", return_value=True):
        out = ce.selinux_overlay([str(fake)], dict(os.environ), dest)
        text = out.read_text() if out else ""
        check("overlay names every service compose lists",
              all(f"  {s}:" in text for s in ("episode", "postgres", "native-services"))
              and text.count("label=disable") == 3, text)
        with mock.patch.dict(os.environ, {"ALKERA_SELINUX_LABEL": "keep"}):
            check("ALKERA_SELINUX_LABEL=keep opts out",
                  ce.selinux_overlay([str(fake)], {}, dest) is None)


def test_release_script(tmp: Path) -> None:
    """The ownership script, run by this user against a tree it owns: it must
    exit 0, walk every root that exists, skip one that does not, and print the
    reference owner. (The uid change itself needs root; the engine supplies it.)"""
    root = tmp / "box"
    for d in ("audit", "world/var", "workspace"):
        (root / d).mkdir(parents=True)
    script = ce.RELEASE_OWNERSHIP.replace("/workspace", f"{root}/workspace") \
        .replace("/world", f"{root}/world").replace("/audit", f"{root}/audit") \
        + f"; [ -e {root}/absent ] && chown -R --reference={root}/audit {root}/absent; true"
    r = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
    check("release script runs", r.returncode == 0, r.stderr)
    check("release script reports the reference owner",
          r.stdout.strip().splitlines()[0] == f"{os.getuid()}:{os.getgid()}", r.stdout)


def test_release_skipped_off_linux() -> None:
    with mock.patch.object(ce.sys, "platform", "win32"):
        check("release is a no-op off Linux", ce.release_ownership("abc") == {"skipped": True})
    check("release is a no-op with no container", ce.release_ownership("") == {"skipped": True})


def test_shell_pinned(tmp: Path) -> None:
    w = ce.write_bash_wrapper(tmp / "shell", container="episode", interpreter="python")
    check("SHELL pinned to the verified bash", w["subject_env"]["SHELL"] == w["launcher"])


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="alkera-linux-") as t:
        tmp = Path(t)
        test_parent_session_stripped()
        test_auth()
        test_selinux_overlay(tmp)
        test_release_script(tmp)
        test_release_skipped_off_linux()
        test_shell_pinned(tmp)
    print(f"\n  {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
