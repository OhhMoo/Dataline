"""minio_containment_check.py's own logic, without docker: the boxes it measures
agree with what the profiles plug, and its in-container probe reports what it
should. The measurement itself is tasks/harness/evidence/minio_containment.json."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import episode  # noqa: E402
import minio_containment_check as check  # noqa: E402


def test_boxes_agree_with_the_profiles():
    """A store-less box's checks expect no MinIO at all: if a profile started to
    plug the object store, the box would have to move to the store checks."""
    names = {box.profile for box in check.BOXES}
    assert {"da/laptop", "da/snowflake", ""} <= names
    for box in check.BOXES:
        if not box.profile:
            assert box.store and not box.notebook
            continue
        profile = episode.environment_profile(box.profile, box.plugs)
        assert box.store == ("object_store" in profile.slots), box
        assert box.notebook == ("notebook" in profile.slots), box


def test_forged_posts_cover_the_obvious_guesses():
    headers = check.forge_headers("wrong")
    assert {} in headers
    assert {"Authorization": check.SHARED_CREDENTIAL} in headers
    assert {"Authorization": "Bearer wrong"} in headers
    event = json.loads(check.forged_event("k.txt"))
    assert event["api"]["name"] == "PutObject" and event["api"]["object"] == "k.txt"


def _probe(spec: dict) -> dict:
    program = f"import json\nSPEC = json.loads({json.dumps(json.dumps(spec))})\n{check.PROBE}"
    done = subprocess.run([sys.executable, "-"], input=program, capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.splitlines()[-1])


def test_probe_reports_resolution_refusal_and_a_planted_token(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "leak.txt").write_text("x deadbeefcafe0123 y")
    (tmp_path / "clean.txt").write_text("nothing here")
    got = _probe({"names": ["localhost"], "forge": [{}],
                  "receiver": "http://127.0.0.1:1/minio/audit",
                  "body": check.forged_event("k"),
                  "token_env": "ALKERA_MINIO_AUDIT_TOKEN", "token_sha256": "0" * 64,
                  "token_prefix": "deadbeefcafe0123", "scan": [str(tmp_path)]})
    assert isinstance(got["resolve"]["localhost"], list)
    assert got["forge"][0]["status"] is None and "refused" in got["forge"][0]["error"]
    assert got["token_in_env"] == []
    assert str(tmp_path / "a" / "b" / "leak.txt") in got["token_scan"]["hits"]
    assert str(tmp_path / "clean.txt") not in got["token_scan"]["hits"]


def test_probe_reports_any_open_port_besides_9000():
    """On a store box MinIO's address is dialed on EVERY TCP port, not only :9000:
    the console MinIO serves unless told not to (`minio:<random port>`,
    CONTAINMENT_2026-10-07.md) is a listener nobody audits, and `only_9000_open`
    has to see it wherever it lands. Here two listeners stand in for the API and
    a console on a random port; the sweep finds both and names the second."""
    import socket

    assert check.FULL_SWEEP == (1, 65535)
    spec = check.store_sweep_spec(["127.0.0.1"])
    assert (spec["port_sweep"]["first"], spec["port_sweep"]["last"]) == check.FULL_SWEEP
    servers = [socket.create_server(("127.0.0.1", 0)) for _ in range(2)]
    try:
        api, console = (s.getsockname()[1] for s in servers)
        got = _probe(spec)
    finally:
        for server in servers:
            server.close()
    sweep = got["port_sweep"]
    assert sweep["dialed"] == 65535
    assert {api, console} <= set(sweep["open"]["127.0.0.1"])
    others = check.other_open_ports(sweep, allowed=api)
    assert console in others["127.0.0.1"] and api not in others["127.0.0.1"]
    assert check.other_open_ports({"open": {"10.0.0.2": [9000]}}) == {}
    assert check.other_open_ports({"open": {"10.0.0.2": [9000, 41045]}}) == \
        {"10.0.0.2": [41045]}
