"""The MinIO audit webhook -> one 9.2.2 row per S3 request.

WHY A WEBHOOK AND NOT A CLIENT WRAPPER
--------------------------------------
`aws s3`, `boto3`, `s3cmd`, `mc` and a hand-rolled `curl` all reach the same
bucket. Wrapping one of them audits one of them. MinIO already emits a signed
record of every request it served, so the row is written by the server that did
the work and no client can be outside it.

HOW THE SERVER IS MADE TO TALK
------------------------------
    MINIO_AUDIT_WEBHOOK_ENABLE_alkera=on
    MINIO_AUDIT_WEBHOOK_ENDPOINT_alkera=http://<receiver>:<port>/audit

MinIO then POSTs one JSON object per request it serves, `api.name` naming the
S3 operation. The receiver is `app` below; `python -m
alkera_gateway.native.tailers.minio --port 9010` runs it.

WHAT IS DROPPED AND WHY
-----------------------
Entries with no `api.name` are health and metrics probes, which are the
container runtime talking to MinIO rather than anything the agent did; they are
skipped. Every named operation is recorded, including ones this module has no
entry for - those get an `op_class` from the verb prefix and the flag
`unclassified_api`, because a `PutObjectRetention` silently logged as a read is
worse than one logged as a write with a note on it.
"""
import argparse
import json
import sys
from typing import Any

from ..audit import record

ACTOR = "minio-tailer"

# The S3 operations an episode's storage surface uses, classified the way
# `alkera_gateway.storage.ObjectStorage` classifies the MCP tools that wrap
# them: a put is a `write` with an overwrite risk, a delete is `destructive`.
OP_CLASS = {
    "GetObject": "read",
    "HeadObject": "read",
    "GetObjectTagging": "read",
    "GetObjectAttributes": "read",
    "ListObjects": "read",
    "ListObjectsV1": "read",
    "ListObjectsV2": "read",
    "ListObjectVersions": "read",
    "ListBuckets": "read",
    "HeadBucket": "read",
    "GetBucketLocation": "read",
    "GetBucketPolicy": "read",
    "GetBucketVersioning": "read",
    "GetBucketTagging": "read",
    "SelectObjectContent": "read",
    "PutObject": "write",
    "CopyObject": "write",
    "PutObjectTagging": "write",
    "NewMultipartUpload": "write",
    "CreateMultipartUpload": "write",
    "PutObjectPart": "write",
    "CompleteMultipartUpload": "write",
    "PutBucketPolicy": "write",
    "PutBucketTagging": "write",
    "PutBucketVersioning": "write",
    # MinIO names bucket creation `PutBucket`; `MakeBucket` is the admin
    # path `mc mb` takes. Measured from the audit webhook, 2026-09-16.
    "PutBucket": "ddl",
    "MakeBucket": "ddl",
    "CreateBucket": "ddl",
    "DeleteObject": "destructive",
    "DeleteObjectTagging": "destructive",
    "DeleteMultipleObjects": "destructive",
    "DeleteBucket": "destructive",
    "DeleteBucketPolicy": "destructive",
    "AbortMultipartUpload": "destructive",
}
# Applied to names the table does not hold, longest prefix first.
PREFIX_OP_CLASS = (
    ("Delete", "destructive"),
    ("Abort", "destructive"),
    ("Remove", "destructive"),
    ("Complete", "write"),
    ("Create", "write"),
    ("Copy", "write"),
    ("New", "write"),
    ("Post", "write"),
    ("Put", "write"),
    ("Select", "read"),
    ("Get", "read"),
    ("Head", "read"),
    ("List", "read"),
)
FALLBACK_OP_CLASS = "exec"
# The flags `ObjectStorage.call` sets, so a native put and an MCP `s3_put` carry
# the same warning.
WRITE_FLAGS = {"write": ["possible_overwrite"], "destructive": ["delete"]}


def classify(name: str) -> tuple[str, list[str]]:
    """S3 operation name -> (op_class, flags)."""
    known = OP_CLASS.get(name)
    if known is not None:
        return known, list(WRITE_FLAGS.get(known, []))
    for prefix, op_class in PREFIX_OP_CLASS:
        if name.startswith(prefix):
            return op_class, [*WRITE_FLAGS.get(op_class, []), "unclassified_api"]
    return FALLBACK_OP_CLASS, ["unclassified_api"]


def target(event: dict[str, Any]) -> str | None:
    """`s3://bucket/key`, the string `ObjectStorage.call` writes as its target.

    A listing names a prefix rather than a key, and MinIO puts the prefix in the
    query rather than in `api.object`, so the query is where it is read from -
    that is what makes `s3_list` and `aws s3 ls` produce the same target.
    """
    api = event.get("api") or {}
    bucket = api.get("bucket")
    if not bucket:
        return None
    key = api.get("object") or ""
    if not key:
        query = event.get("requestQuery") or {}
        key = query.get("prefix") or ""
    return f"s3://{bucket}/{key}"


def latency_ms(event: dict[str, Any]) -> float:
    """`timeToResponseInNS` in milliseconds; 0.0 when the server did not say."""
    api = event.get("api") or {}
    try:
        return round(int(api.get("timeToResponseInNS") or 0) / 1_000_000, 3)
    except (TypeError, ValueError):
        return 0.0


def row_from_event(event: dict[str, Any]) -> dict | None:
    """One audit entry -> one 9.2.2 row, or None for a probe with no operation."""
    api = event.get("api") or {}
    name = api.get("name")
    if not name:
        return None
    op_class, flags = classify(name)
    touched = target(event)
    targets = [touched] if touched else []
    status_code = api.get("statusCode") or 0
    failed = not (200 <= int(status_code) < 300)
    args = {
        "bucket": api.get("bucket") or "",
        "key": api.get("object") or "",
        "request_path": event.get("requestPath") or "",
        "user_agent": event.get("userAgent") or "",
    }
    return record(
        tool=f"minio.{name}",
        args=args,
        op_class=op_class,
        targets=targets,
        read_targets=targets if op_class == "read" else [],
        exit="error" if failed else "ok",
        error={"code": f"s3_{status_code}", "message": api.get("status") or ""}
        if failed
        else None,
        latency_ms=latency_ms(event),
        result={"status": api.get("status"), "status_code": status_code,
                "rx": api.get("rx"), "tx": api.get("tx")},
        flags=flags,
        actor=ACTOR,
    )


def build_app():
    """The receiver.

    FastAPI is imported here, not at module scope, because it is the `native`
    extra and the parse functions above must stay importable on a base install.
    That is also why this file does not use `from __future__ import annotations`:
    FastAPI resolves a handler's annotations against module globals, so a
    stringised `Request` imported into a function scope is unresolvable and the
    handler is served as a 422 - measured against MinIO before it was fixed.
    """
    from fastapi import FastAPI, Request

    application = FastAPI(title="alkera minio audit receiver")

    @application.post("/{path:path}")
    async def audit(request: Request):
        """MinIO posts one JSON object per request it served."""
        body = await request.body()
        written = 0
        for line in body.decode("utf-8", "replace").splitlines():
            if line.strip():
                written += row_from_event(json.loads(line)) is not None
        return {"rows": written}

    return application


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9010)
    arguments = parser.parse_args(argv)
    print(
        f"MINIO_AUDIT_WEBHOOK_ENDPOINT_alkera=http://{arguments.host}:{arguments.port}/audit",
        file=sys.stderr,
    )
    uvicorn.run(build_app(), host=arguments.host, port=arguments.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
