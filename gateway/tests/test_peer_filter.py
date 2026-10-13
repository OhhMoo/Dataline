"""serve.py's agent listeners admit episode peers only.

When a hosted engine is plugged, native-services joins backend_net, and every agent
listener (bound on 0.0.0.0) answers on that network too. MinIO, a backend_net peer
the box's root credential can drive, then dials them for the agent, and each dial was
counted as the episode's own destination (gateway/docker/evidence/PEERS_2026-10-13.md).
With ALKERA_EPISODE_SUBNETS set, a peer outside those subnets is refused at accept:
one `native.refused_peer` row each, never a destination count.

Loopback stands in for the two networks: 127.0.0.1 is the episode's address, and a
client bound to 127.0.0.2 is a backend_net peer.

    cd gateway && .venv/bin/python -m pytest -q tests/test_peer_filter.py
"""
from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.request

import pytest
from test_tls_destinations import bare_dial, count, free_port, serving, settled, tls_door

from alkera_gateway.native import serve

EPISODE = [ipaddress.IPv4Network("127.0.0.1/32")]


@pytest.fixture
def actions(tmp_path, monkeypatch):
    path = tmp_path / "audit" / "actions.jsonl"
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(path))
    return path


def rows(path, tool=serve.REFUSED_PEER_TOOL):
    if not path.exists():
        return []
    return [r for r in (json.loads(x) for x in path.read_text().splitlines() if x)
            if r["tool"] == tool]


def dial_from(source: str, port: int) -> bytes:
    """A TCP dial from `source`, then whatever the listener sends before it closes."""
    with socket.socket() as client:
        client.settimeout(10)
        client.bind((source, 0))
        client.connect(("127.0.0.1", port))
        client.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        try:
            return client.recv(4096)
        except ConnectionResetError:
            return b""


def plain(name="plain", destination=True) -> serve.Mounted:
    return serve.Mounted(name, f"{name}.internal", free_port(), serve.stub_app(name),
                         "stub", False, destination=destination)


def test_peer_allowed_is_membership_of_the_episode_subnets():
    allowed = serve.episode_subnets("10.210.7.0/24, 10.210.9.0/24")
    assert allowed == (ipaddress.IPv4Network("10.210.7.0/24"),
                       ipaddress.IPv4Network("10.210.9.0/24"))
    assert serve.peer_allowed("10.210.7.12", allowed)
    assert serve.peer_allowed("::ffff:10.210.9.3", allowed)   # a v4-mapped peer
    assert not serve.peer_allowed("10.210.8.5", allowed)       # backend_net
    assert not serve.peer_allowed("127.0.0.1", allowed)
    assert not serve.peer_allowed("not-an-address", allowed)
    assert serve.episode_subnets("") == ()
    with pytest.raises(ValueError, match="ALKERA_EPISODE_SUBNETS"):
        serve.episode_subnets("10.210.7.0/33")


def test_peer_outside_episode_subnet_is_refused_and_recorded(tmp_path, actions):
    agent = plain()
    receiver = plain("receiver", destination=False)  # MinIO's, not the agent's
    log = serve.DestinationLog(tmp_path / "destinations.jsonl")
    log.declare(agent.host, agent.port)
    with serving([agent, receiver], log,
                 allowed=[ipaddress.IPv4Network("10.250.0.0/24")]):
        assert dial_from("127.0.0.1", agent.port) == b""      # closed, never answered
        # a listener that is not the agent's is not filtered
        assert dial_from("127.0.0.1", receiver.port).startswith(b"HTTP/1.1 200")
        time.sleep(0.5)
        assert count(log, agent.port) == 0
    refused = rows(actions)
    assert len(refused) == 1
    row = refused[0]
    assert row["args"] == {"peer": "127.0.0.1", "host": agent.host, "port": agent.port}
    assert row["targets"] == [f"{agent.host}:{agent.port}"]
    assert row["exit"] == "error" and row["error"]["code"] == "refused_peer"


def test_episode_peer_accepted(tmp_path, actions):
    agent = plain()
    log = serve.DestinationLog(tmp_path / "destinations.jsonl")
    log.declare(agent.host, agent.port)
    with serving([agent], log, allowed=EPISODE):
        request = urllib.request.Request(f"http://127.0.0.1:{agent.port}/health",
                                         headers={"Connection": "close"})
        with urllib.request.urlopen(request, timeout=15) as answer:
            assert answer.status == 200
        assert settled(log, agent.port, at_least=1) == 1
    assert rows(actions) == []


def test_backend_net_peer_dial_is_not_a_destination(tmp_path, actions):
    """On a TLS door, where Task 5 counts at accept, before the handshake: the refusal
    comes first, so a backend_net peer's dial is a row and never a count, while the
    episode's own bare dial still is one."""
    item, log, _ = tls_door(tmp_path)
    with serving([item], log, allowed=EPISODE):
        assert dial_from("127.0.0.2", item.port) == b""
        time.sleep(0.5)
        assert count(log, item.port) == 0
        bare_dial(item.port)                                  # from 127.0.0.1
        assert settled(log, item.port, at_least=1) == 1
    refused = rows(actions)
    assert [r["args"]["peer"] for r in refused] == ["127.0.0.2"]
