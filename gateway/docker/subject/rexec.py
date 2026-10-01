"""The subject's shell, carried from the subject container into the episode.

Under the hard sandbox (tasks/harness/README.md#the-subject-sandbox) the model's CLI runs in the
`subject` container and its Bash tool must still act on the episode container,
where the world, the shims and the *.internal services are. `docker exec` from
inside the subject would need the engine socket, which is root on the host, so
the command crosses on a TCP connection over `subject_net` (internal, two
members) instead:

    subject: bash -c CMD -> BASH_ENV hook -> rexec.py client CMD
                                                  | subject_net
    episode:                  rexec.py serve  ->  bash -c CMD   (cwd /workspace)

One file, both ends, stdlib only: it runs on the episode image and the subject
image, which share a Python base.

Wire format. Client -> server: one JSON line {"token", "cmd"}. Server -> client:
frames of 1 type byte + 4-byte big-endian length + payload; b"o" stdout, b"e"
stderr, b"x" the exit status as a signed 4-byte int, then close. A dropped
connection kills the command's whole process group: the CLI's timeout kills the
client, and the command must not outlive it (the same contract `docker exec`
had).

The token keeps a stray connection on the network from running commands. It is
not a secret from the agent: the agent already runs commands here.

The world clock (dynamic tasks only; tasks/harness/dynamic.py). `serve --clock
stdio` is started by the harness over an ATTACHED `docker exec -i`, and before
every authenticated request it writes `tick? <seq> <inflight>` on its own
stdout - the harness's pipe - and waits for `go <seq>` on stdin, under one lock,
before the command starts. The harness advances the host-side world in between,
so the count is the model's commands and no command starts while the world
moves. Without `--clock` nothing here writes to stdout or reads stdin, and a
static task's bridge is the bridge it always was. A harness that is gone (EOF)
or answers out of turn fails the request closed (status 125) rather than
running a command the world clock never saw.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

#: How long a command's pipes may stay open after bash exits (a backgrounded
#: child holding stdout). `docker exec` waited forever; a bounded wait keeps a
#: stray `sleep 1000 &` from hanging the model's turn until the CLI's timeout.
DRAIN_AFTER_EXIT_S = 2.0


def _frame(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack(">I", len(payload)) + payload


# ------------------------------------------------------------------ server
class ClockGate:
    """The harness's say-so before each command (`--clock stdio`)."""

    def __init__(self, rfile, wfile) -> None:
        self.rfile, self.wfile = rfile, wfile
        self.lock = threading.Lock()
        self.seq = 0
        self.inflight = 0
        self.count_lock = threading.Lock()

    def admit(self) -> bool:
        with self.lock:
            self.seq += 1
            try:
                self.wfile.write(f"tick? {self.seq} {self.inflight}\n".encode())
                self.wfile.flush()
                answer = self.rfile.readline().decode("utf-8", "replace").strip()
            except (OSError, ValueError):
                return False
            if answer != f"go {self.seq}":
                return False
            with self.count_lock:
                self.inflight += 1
            return True

    def done(self) -> None:
        with self.count_lock:
            self.inflight -= 1


def _handle(conn: socket.socket, token: str, workdir: str,
            gate: ClockGate | None = None) -> None:
    lock = threading.Lock()
    admitted: ClockGate | None = None

    def send(kind: bytes, payload: bytes) -> None:
        with lock:
            conn.sendall(_frame(kind, payload))

    try:
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
        request = json.loads(buf.split(b"\n", 1)[0])
        if not hmac.compare_digest(str(request.get("token", "")), token):
            send(b"e", b"rexec: bad token\n")
            send(b"x", struct.pack(">i", 126))
            return
        # BASH_ENV is the SUBJECT side's redirect; inherited here it would send
        # the command straight back (measured, with the subject image standing
        # in for the episode). The episode's bash reads no hook.
        env = {k: v for k, v in os.environ.items()
               if k not in ("BASH_ENV", "ALKERA_REXEC_TOKEN")}
        if gate is not None:
            if not gate.admit():
                send(b"e", b"rexec: the world clock did not answer\n")
                send(b"x", struct.pack(">i", 125))
                return
            admitted = gate
        proc = subprocess.Popen(["bash", "-c", request["cmd"]], cwd=workdir, env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)

        def pump(stream, kind: bytes) -> None:
            for chunk in iter(lambda: stream.read1(65536), b""):
                try:
                    send(kind, chunk)
                except OSError:
                    return

        readers = [threading.Thread(target=pump, args=(proc.stdout, b"o"), daemon=True),
                   threading.Thread(target=pump, args=(proc.stderr, b"e"), daemon=True)]
        for r in readers:
            r.start()

        def watch_peer() -> None:
            # Any byte or EOF from the client means it is gone: kill the group.
            try:
                conn.recv(1)
            except OSError:
                pass
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass

        threading.Thread(target=watch_peer, daemon=True).start()
        rc = proc.wait()
        deadline = time.time() + DRAIN_AFTER_EXIT_S
        for r in readers:
            r.join(timeout=max(0.0, deadline - time.time()))
        send(b"x", struct.pack(">i", rc if rc >= 0 else 128 - rc))
    except (OSError, ValueError, KeyError):
        pass
    finally:
        if admitted is not None:
            admitted.done()
        try:
            conn.close()
        except OSError:
            pass


def serve(port: int, token: str, workdir: str, gate: ClockGate | None = None) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(64)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=_handle, args=(conn, token, workdir, gate),
                         daemon=True).start()


# ------------------------------------------------------------------ client
def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("rexec: connection closed mid-frame")
        data += chunk
    return data


def client(cmd: str) -> int:
    host, _, port = os.environ["ALKERA_REXEC_ADDR"].rpartition(":")
    token = os.environ["ALKERA_REXEC_TOKEN"]
    last = None
    for _ in range(20):                    # the server may still be starting
        try:
            sock = socket.create_connection((host, int(port)), timeout=10)
            break
        except OSError as exc:
            last = exc
            time.sleep(0.5)
    else:
        sys.stderr.write(f"rexec: cannot reach the episode at {host}:{port}: {last}\n")
        return 125
    sock.settimeout(None)
    sock.sendall(json.dumps({"token": token, "cmd": cmd}).encode() + b"\n")
    out, err = sys.stdout.buffer, sys.stderr.buffer
    try:
        while True:
            kind = _recv_exact(sock, 1)
            (length,) = struct.unpack(">I", _recv_exact(sock, 4))
            payload = _recv_exact(sock, length)
            if kind == b"x":
                return struct.unpack(">i", payload)[0]
            target = out if kind == b"o" else err
            target.write(payload)
            target.flush()
    except ConnectionError as exc:
        err.write(f"{exc}\n".encode())
        return 125


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rexec")
    sub = parser.add_subparsers(dest="mode", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--workdir", default="/workspace")
    s.add_argument("--clock", choices=["stdio"], default=None,
                   help="dynamic tasks: ask the harness on stdout/stdin before "
                        "every command (see the module docstring)")
    c = sub.add_parser("client")
    c.add_argument("cmd")
    args = parser.parse_args(argv)
    if args.mode == "serve":
        # The token comes from the environment, not argv, so `ps` in the
        # episode does not print it next to the command line.
        gate = (ClockGate(sys.stdin.buffer, sys.stdout.buffer)
                if args.clock == "stdio" else None)
        serve(args.port, os.environ["ALKERA_REXEC_TOKEN"], args.workdir, gate)
        return 0
    return client(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
