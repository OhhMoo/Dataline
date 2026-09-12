import base64
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID


def json_value(value):
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return {"type": "float", "value": str(value)}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return {"type": "bytes", "base64": base64.b64encode(value).decode("ascii")}
    return value


def canonical(value) -> bytes:
    return json.dumps(
        json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass
class Observation:
    op_class: str = "read"
    targets: list[str] = field(default_factory=list)
    read_targets: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    rows_affected: int | None = None


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def operation_record(row: dict) -> dict:
    keys = (
        "seq", "vts", "tool", "args", "args_hash", "op_class", "targets", "read_targets",
        "flags", "rows_affected", "result", "result_hash", "exit", "error",
    )
    return {key: row[key] for key in keys}

