import json
from pathlib import Path

from .audit import validate_trace
from .records import digest


def replay(source: Path, gateway):
    if source.resolve() == gateway.config.audit_dir:
        raise ValueError("Replay needs a separate audit directory")
    if gateway.audit.seq:
        raise ValueError("Replay needs a fresh destination episode")
    manifest = json.loads((source / "session.json").read_text(encoding="utf-8"))
    destination = json.loads((gateway.config.audit_dir / "session.json").read_text(encoding="utf-8"))
    if not manifest["initial_world_fp"]:
        raise ValueError("Source session has no initial world fingerprint")
    if manifest["initial_world_fp"] != destination["initial_world_fp"]:
        raise ValueError("Restore the source world's initial snapshot before replay")
    if manifest["versions"] != destination["versions"]:
        raise ValueError("Replay dependency versions differ")
    rows = validate_trace(source / "actions.jsonl")
    differences = []
    for row in rows:
        if gateway.clock() != row["vts"]:
            raise ValueError(f"Virtual clock differs before call {row['seq']}")
        response = gateway.call(row["tool"], row["args"], "replay")
        actual = {"result": response["result"], "error": response["error"]}
        expected = {"result": row["result"], "error": row["error"]}
        if digest(actual) != digest(expected):
            differences.append({"seq": row["seq"], "tool": row["tool"]})
    return {
        "calls": len(rows), "responses_match": not differences, "differences": differences,
        "final_world_verified": False,
    }
