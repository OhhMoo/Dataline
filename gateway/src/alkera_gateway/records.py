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
        json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


# The fields a module method assigns to say what an action did. Assigning any
# of them is what `classified` means. `flags` is deliberately not one: a flag
# describes an action, it does not assert that one happened.
CLASSIFIERS = ("op_class", "targets", "read_targets", "rows_affected")


class SealedObservation(RuntimeError):
    """A second operation tried to reclassify an observation already closed.

    Raised rather than silently accepted because the losing classification is
    the one a grader would have read: an endpoint that runs two module calls
    against one observation ends up with a row naming whatever it touched last.
    """


class UnclassifiedObservation(RuntimeError):
    """A row was asked for before any module method said what the action was.

    The native services obtain `op_class` and `targets` from the observation the
    module filled in, never from a literal in a handler. A handler that answers
    a request without running a module call therefore has nothing to write, and
    saying so loudly is what stops a new endpoint from shipping unclassified.
    """


@dataclass
class Observation:
    """What one action touched, filled in by the module method that ran it.

    `Gateway._call` creates one of these per tool call, hands it to the
    `Operations` method and reads the row's classification back off it. The
    native services do exactly the same, which is how the two transports agree
    about an action without a table that maps one to the other.

    SEALING AND NESTING exist for the composed endpoints. A vendor-shaped
    request often runs more than one module call: a JQL search is a listing plus
    a fetch per row, a Confluence `PUT` reads the current version before it
    writes. One observation across all of them would carry whatever the last
    call set, so:

    - `child()` opens a fresh observation for a nested call. Its classification
      is its own and cannot overwrite the caller's.
    - `seal()` closes an observation the moment the call that defines the action
      returns. Anything that tries to reclassify it afterwards raises.
    """

    op_class: str = "read"
    targets: list[str] = field(default_factory=list)
    read_targets: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    rows_affected: int | None = None
    # Bookkeeping, not part of the row and not part of equality: `Observation`
    # instances are compared by what they observed, which these two do not
    # change. They are class-level defaults so `__setattr__` can read them
    # while the generated `__init__` is still assigning the fields above.
    classified: bool = field(default=False, init=False, repr=False, compare=False)
    sealed: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self):
        # `__setattr__` marks every assignment made by `__init__` as a
        # classification; an observation is only really classified at birth if
        # a caller passed something other than the defaults.
        object.__setattr__(
            self,
            "classified",
            self.op_class != "read"
            or bool(self.targets)
            or bool(self.read_targets)
            or self.rows_affected is not None,
        )

    def __setattr__(self, name, value):
        if name in CLASSIFIERS:
            if self.sealed:
                raise SealedObservation(
                    f"this observation is sealed; {name} was already decided by the "
                    f"operation that defines this action (op_class={self.op_class!r}, "
                    f"targets={self.targets!r}). Use child() for a nested call."
                )
            object.__setattr__(self, "classified", True)
        object.__setattr__(self, name, value)

    def seal(self) -> "Observation":
        """Close this observation: what it says is now the row's, for good."""
        object.__setattr__(self, "sealed", True)
        return self

    def child(self) -> "Observation":
        """A fresh observation for a nested call, detached from this one.

        Detached rather than linked: the header and precondition reads a vendor
        shape forces are not part of the action being recorded, so what they
        touch must not reach the row at all (HUMAN_PLANE.md rule 3).
        """
        return Observation()


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def operation_record(row: dict) -> dict:
    keys = (
        "seq",
        "vts",
        "tool",
        "args",
        "args_hash",
        "op_class",
        "targets",
        "read_targets",
        "flags",
        "rows_affected",
        "result",
        "result_hash",
        "exit",
        "error",
    )
    return {key: row[key] for key in keys}
