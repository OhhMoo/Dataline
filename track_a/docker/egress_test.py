"""Egress gate: every outbound attempt from the episode zone must fail.
Run inside the episode container. Exit 0 iff egress is fully blocked."""
import socket
import sys

failures = []
for host, port in [("8.8.8.8", 53), ("1.1.1.1", 443)]:
    try:
        socket.create_connection((host, port), timeout=3).close()
        failures.append(f"tcp {host}:{port} CONNECTED")
    except OSError:
        pass
try:
    socket.getaddrinfo("example.com", 443)
    failures.append("dns example.com RESOLVED")
except OSError:
    pass

if failures:
    print("egress test: FAIL — " + "; ".join(failures))
    sys.exit(1)
print("egress test: PASS — no tcp, no dns")
