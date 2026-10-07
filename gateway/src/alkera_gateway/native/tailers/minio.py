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
S3 operation. The receiver is `build_app` below; `ALKERA_MINIO_AUDIT_TOKEN=<token>
python -m alkera_gateway.native.tailers.minio --port 9010` runs it.

WHO MAY POST
------------
Only MinIO. The receiver shares a network with the episode (native-services is
on episode_net, which is how MinIO reaches it), so without a check anything the
agent runs could POST a made-up event and forge `minio.*` rows. MinIO is given
a shared secret,

    MINIO_AUDIT_WEBHOOK_AUTH_TOKEN_alkera=<token>

which it sends, bare, as the `Authorization` header of every webhook POST
(measured on the pinned pgsty/minio, RELEASE.2026-08-04), and the
receiver is built with the same token (`build_app(token)`; serve.py reads it
from TOKEN_ENV). A POST without it is answered 401 before its body is read, and
writes no row. There is no unauthenticated mode: `build_app` refuses an empty
token, and serve.py then serves no receiver and says so.

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
import hmac
import json
import os
import sys
from typing import Any

from ..audit import record

ACTOR = "minio-tailer"
#: Where serve.py (and `main` below) read the shared secret from. Set on the
#: native-services container by compose.native.yml, from the per-rollout value
#: tasks/harness/container_episode.NativeContainerEpisode mints.
TOKEN_ENV = "ALKERA_MINIO_AUDIT_TOKEN"
#: What a refused POST is answered. No row is written for it.
UNAUTHORIZED = {"error": "unauthorized",
                "message": "the MinIO audit receiver accepts MinIO's webhook only"}

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


def authorized(header: str | None, token: str) -> bool:
    """Does an `Authorization` header carry `token`?

    Accepted as MinIO may send it: the bare token, or `Bearer <token>`. Compared
    in constant time. An empty token authorizes nothing.
    """
    if not token or not header:
        return False
    value = header.strip()
    scheme, _, rest = value.partition(" ")
    if scheme.lower() == "bearer" and rest.strip():
        value = rest.strip()
    return hmac.compare_digest(value.encode("utf-8"), token.encode("utf-8"))


def build_app(token: str):
    """The receiver, answering only POSTs that carry `token` (see WHO MAY POST).

    FastAPI is imported here, not at module scope, because it is the `native`
    extra and the parse functions above must stay importable on a base install.
    That is also why this file does not use `from __future__ import annotations`:
    FastAPI resolves a handler's annotations against module globals, so a
    stringised `Request` imported into a function scope is unresolvable and the
    handler is served as a 422 - measured against MinIO before it was fixed.
    """
    if not token:
        raise ValueError(
            f"the MinIO audit receiver needs a token ({TOKEN_ENV}): without one, "
            f"anything that can reach it could append minio.* rows")
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    application = FastAPI(title="alkera minio audit receiver")

    @application.post("/{path:path}")
    async def audit(request: Request):
        """MinIO posts one JSON object per request it served."""
        if not authorized(request.headers.get("authorization"), token):
            # Refused before the body is read: nothing of it reaches a row.
            # Said on stderr (the container's log), not in actions.jsonl.
            client = request.client.host if request.client else "?"
            print(f"minio audit receiver: refused a POST from {client} "
                  f"(no valid token)", file=sys.stderr, flush=True)
            return JSONResponse(UNAUTHORIZED, status_code=401)
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
    token = os.environ.pop(TOKEN_ENV, "")
    if not token:
        print(f"set {TOKEN_ENV}, and give MinIO the same value as "
              f"MINIO_AUDIT_WEBHOOK_AUTH_TOKEN_alkera", file=sys.stderr)
        return 2
    print(
        f"MINIO_AUDIT_WEBHOOK_ENDPOINT_alkera=http://{arguments.host}:{arguments.port}/audit",
        file=sys.stderr,
    )
    uvicorn.run(build_app(token), host=arguments.host, port=arguments.port,
                log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
