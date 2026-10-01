"""The hard sandbox's two moving parts, on loopback, without an engine or a model.

    python tasks/harness/test_subject_sandbox_pure.py

rexec.py (the command bridge) and egress_proxy.py (the allowlist) are run as
real processes on 127.0.0.1 and driven the way the subject container drives
them. The containers around them are `test_subject_sandbox.py` (live).
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUBJECT = HERE.parents[1] / "gateway" / "docker" / "subject"
REXEC = SUBJECT / "rexec.py"
PROXY = SUBJECT / "egress_proxy.py"
HOOK = SUBJECT / "bash_env.sh"

PASSED = FAILED = 0


def check(name: str, ok: bool, detail: object = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name} {detail}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_listening(port: int) -> None:
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"nothing listening on {port}")


def client(cmd: str, env: dict, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(REXEC), "client", cmd], env=env,
                          capture_output=True, text=True, timeout=timeout)


def test_rexec(tmp: Path) -> None:
    port, token = free_port(), "t0k"
    workdir = tmp / "workspace"
    workdir.mkdir()
    server = subprocess.Popen(
        [sys.executable, str(REXEC), "serve", "--port", str(port),
         "--workdir", str(workdir)],
        env={**os.environ, "ALKERA_REXEC_TOKEN": token})
    try:
        wait_listening(port)
        env = {**os.environ, "ALKERA_REXEC_ADDR": f"127.0.0.1:{port}",
               "ALKERA_REXEC_TOKEN": token}
        r = client("echo out; echo err >&2; exit 7", env)
        check("stdout, stderr and the exit status cross separately",
              (r.stdout, r.stderr, r.returncode) == ("out\n", "err\n", 7),
              (r.stdout, r.stderr, r.returncode))
        r = client("pwd", env)
        check("commands run in the server's workdir", r.stdout.strip() == str(workdir),
              r.stdout)
        r = client("head -c 3000000 /dev/zero | wc -c", env)
        check("a large output crosses intact", r.stdout.strip() == "3000000", r.stdout)
        r = client("kill -9 $$", env)
        check("death by signal reads as 128+signal", r.returncode == 137, r.returncode)
        r = client("echo hi", {**env, "ALKERA_REXEC_TOKEN": "wrong"})
        check("a wrong token runs nothing", r.returncode == 126 and "bad token" in r.stderr,
              (r.returncode, r.stderr))
        marker = tmp / "survivor"
        try:
            client(f"sleep 3; touch {marker}", env, timeout=1)
        except subprocess.TimeoutExpired:
            pass                                # subprocess.run killed the client
        time.sleep(4)
        check("a dropped client kills the command it started", not marker.exists())
        t0 = time.time()
        r = client("sleep 30 & echo started", env)
        check("a backgrounded child does not hold the turn open",
              r.stdout.strip() == "started" and time.time() - t0 < 10,
              round(time.time() - t0, 1))
    finally:
        server.kill()


def test_rexec_clock(tmp: Path) -> None:
    """`serve --clock stdio` (dynamic tasks): one `tick? <seq> <inflight>` line
    per authenticated command BEFORE it runs, the command held until `go <seq>`,
    and a harness that answers wrongly or not at all fails the command closed."""
    port, token = free_port(), "t0k"
    workdir = tmp / "workspace-clock"
    workdir.mkdir()
    server = subprocess.Popen(
        [sys.executable, str(REXEC), "serve", "--port", str(port),
         "--workdir", str(workdir), "--clock", "stdio"],
        env={**os.environ, "ALKERA_REXEC_TOKEN": token},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    seen: list[str] = []
    marker = workdir / "ran"
    try:
        wait_listening(port)
        env = {**os.environ, "ALKERA_REXEC_ADDR": f"127.0.0.1:{port}",
               "ALKERA_REXEC_TOKEN": token}
        r = client("echo hi", {**env, "ALKERA_REXEC_TOKEN": "wrong"})
        ok = r.returncode == 126
        check("a bad token asks the clock nothing", ok, r.returncode)
        assert ok

        def harness(answer):
            line = server.stdout.readline().decode().strip()
            seen.append(line)
            # The command must not have started while the harness holds it.
            seen.append("held" if not marker.exists() else "ran-early")
            server.stdin.write((answer(line) + "\n").encode())
            server.stdin.flush()

        import threading
        for n in (1, 2):
            t = threading.Thread(target=harness,
                                 args=(lambda line: "go " + line.split()[1],))
            t.start()
            r = client(f"touch {marker}; echo n{n}", env)
            t.join(10)
            ok = r.stdout.strip() == f"n{n}" and r.returncode == 0
            check(f"command {n} runs after `go {n}`", ok, (r.stdout, r.returncode))
            assert ok
            marker.unlink()
        ok = seen == ["tick? 1 0", "held", "tick? 2 0", "held"]
        check("one control line per command, before it runs", ok, seen)
        assert ok
        t = threading.Thread(target=harness, args=(lambda line: "go 99",))
        t.start()
        r = client(f"touch {marker}", env)
        t.join(10)
        ok = r.returncode == 125 and not marker.exists() \
            and "world clock" in r.stderr
        check("an out-of-turn answer fails the command closed", ok,
              (r.returncode, r.stderr))
        assert ok
        server.stdin.close()                         # the harness is gone
        r = client(f"touch {marker}", env)
        ok = r.returncode == 125 and not marker.exists()
        check("no harness, no command", ok, (r.returncode, r.stderr))
        assert ok
    finally:
        server.kill()


def test_hook(tmp: Path) -> None:
    """bash_env.sh, as the subject's bash reads it: the CLI's cwd is rewritten
    and the command goes to rexec. rexec is replaced here by a stand-in python
    that prints its argv, so what crosses is visible."""
    fake = tmp / "fake"
    (fake / "opt" / "alkera").mkdir(parents=True)
    hook = HOOK.read_text().replace("/usr/local/bin/python", sys.executable) \
        .replace("/opt/alkera/rexec.py", str(fake / "echo.py"))
    (fake / "hook.sh").write_text(hook)
    (fake / "echo.py").write_text("import sys; print(sys.argv[1:])\n")
    cwd = tmp / "alkera-subject-cwd"
    cwd.mkdir()
    env = {**os.environ, "BASH_ENV": str(fake / "hook.sh"),
           "ALKERA_SUBJECT_CWD": str(cwd)}
    r = subprocess.run(["bash", "-c", f"cd {cwd} && ls /workspace-not-touched"],
                       env=env, capture_output=True, text=True, cwd=cwd)
    check("the hook hands the command to rexec with the CLI's cwd rewritten",
          r.stdout.strip() == "['client', 'cd /workspace && ls /workspace-not-touched']",
          r.stdout + r.stderr)


def test_proxy() -> None:
    port = free_port()
    upstream = socket.socket()
    upstream.bind(("127.0.0.1", 0))
    upstream.listen(4)
    up_port = upstream.getsockname()[1]
    proxy = subprocess.Popen(
        [sys.executable, str(PROXY)],
        env={**os.environ, "ALKERA_EGRESS_PORT": str(port),
             "ALKERA_EGRESS_ALLOW": f"127.0.0.1:{up_port}"},
        stdout=subprocess.PIPE, text=True)
    try:
        wait_listening(port)

        def ask(request: bytes) -> tuple[str, socket.socket]:
            s = socket.create_connection(("127.0.0.1", port), 5)
            s.sendall(request)
            return s.recv(200).split(b"\r\n", 1)[0].decode(), s

        line, s = ask(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        s.close()
        check("CONNECT off the allowlist is refused", " 403 " in line, line)
        line, s = ask(b"GET http://127.0.0.1/ HTTP/1.1\r\nHost: x\r\n\r\n")
        s.close()
        check("plain HTTP is refused, even to an allowed host", " 403 " in line, line)
        line, s = ask(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
        conn, _ = upstream.accept()
        s.sendall(b"ping")
        got = conn.recv(10)
        conn.sendall(b"pong")
        back = s.recv(10)
        s.close()
        conn.close()
        check("CONNECT on the allowlist is tunnelled both ways",
              " 200 " in line and got == b"ping" and back == b"pong", (line, got, back))
    finally:
        proxy.kill()
        out = proxy.communicate(timeout=5)[0]
        upstream.close()
    check("every connection is logged, refused ones included",
          out.count('"event": "refused"') == 2, out)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="alkera-sandbox-") as t:
        tmp = Path(t)
        print("rexec.py")
        test_rexec(tmp)
        print("rexec.py --clock stdio")
        test_rexec_clock(tmp)
        print("bash_env.sh")
        test_hook(tmp)
    print("egress_proxy.py")
    test_proxy()
    print(f"\n  {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
