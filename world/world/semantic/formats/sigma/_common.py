"""Identifiers, names and constants every Sigma module shares.

IDS. A Sigma document is full of opaque ids: UUIDs for workbooks, data models,
connections and folders; 10-character base62 ids for pages, elements, columns,
metrics, groupings and filters; 22-character base62 url ids; 29-character
member ids; and, for a column read straight from a warehouse table,
`inode-<22 chars>/<WAREHOUSE_COLUMN>` (help.sigmacomputing.com/docs/
example-representation-data-model-with-a-table-and-a-relationship). Every one
written here is a pure function of names, so `write_project` is deterministic
and a rebuilt world carries the same ids (BI_BUILD_PLAN.md "Determinism of
vendor files"). Nothing reads a clock: document timestamps are the constant
EPOCH, and the fake service stamps later versions with the world's virtual
clock, never the host's.

NAMES. Sigma titles a warehouse column from its identifier: ORDER_AMOUNT is
shown as "Order Amount", DAY_OF_WEEK as "Day of Week", IATA_CODE as "Iata Code"
(all three from the documented example representations). `display_name` is
that rule; `warehouse_name` is its inverse for files whose column ids do not
carry the warehouse name (ids are random once a column has been edited in the
UI - docs/format-columns-and-metrics-in-the-code-representation-of-a-data-model).
The small-word list is inferred from "Day of Week"; nothing else in the
documented examples exercises it, so it is a reconstruction, not a quotation.
"""
from __future__ import annotations

import hashlib
import re
import uuid

from ...errors import ModelError

#: Sigma's base62 alphabet, as the ids in the documented examples use it.
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

#: The one schema version Sigma's code representation has (OpenAPI
#: `schemaVersion: enum [1]`, Sigma Computing Public REST API 2.0.0+20260930).
SCHEMA_VERSION = 1

#: Constant document timestamps (see the module docstring).
EPOCH = "2026-01-05T09:00:00.000Z"

#: The organisation, owner and folder every written document belongs to.
ORG_SLUG = "alkera"
APP_URL = f"https://app.sigmacomputing.com/{ORG_SLUG}"
FOLDER_PATH = "Analytics"

#: The warehouse connection. Sigma runs on a cloud warehouse; the world's slot
#: for it is Snowflake (plugs.py: `da/snowflake` defaults to Sigma), whose
#: unquoted identifiers are upper case, so paths are written
#: ["ANALYTICS", "WAREHOUSE", "FCT_ORDERS"] for the model source
#: "warehouse.fct_orders". Reading drops the database and lower-cases.
CONNECTION_NAME = "Snowflake"
CONNECTION_TYPE = "snowflake"
DATABASE = "ANALYTICS"

#: How a custom SQL element's source is named in its column formulas
#: (`[Custom SQL/START_DATE]` - docs/example-representation-data-model-with-a-
#: custom-sql-element). Its output columns keep their raw names.
CUSTOM_SQL = "Custom SQL"

#: The prefix of a metric reference (`[Metrics/Unique Users]` -
#: docs/use-metrics-in-a-workbook).
METRICS = "Metrics"


def _digest(*parts: str) -> bytes:
    return hashlib.sha256("\x00".join(("sigma",) + parts).encode("utf-8")).digest()


def base62(*parts: str, length: int = 10) -> str:
    number = int.from_bytes(_digest(*parts), "big")
    out = []
    for _ in range(length):
        number, rest = divmod(number, 62)
        out.append(_ALPHABET[rest])
    return "".join(out)


def stable_uuid(*parts: str) -> str:
    return str(uuid.UUID(bytes=_digest(*parts)[:16], version=4))


ORGANIZATION_ID = stable_uuid("organization", ORG_SLUG)
OWNER_ID = base62("member", "data-engineering", length=29)
FOLDER_ID = stable_uuid("folder", FOLDER_PATH)
CONNECTION_ID = stable_uuid("connection", CONNECTION_NAME)


def slug(name: str) -> str:
    """A file name and url fragment for a document name."""
    text = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not text:
        raise ModelError(f"cannot derive a file name from {name!r}")
    return text


_SMALL_WORDS = frozenset({"a", "an", "and", "as", "at", "by", "for", "in", "of", "on",
                          "or", "the", "to"})


def display_name(warehouse_column: str) -> str:
    """ORDER_AMOUNT -> "Order Amount"; DAY_OF_WEEK -> "Day of Week"."""
    words = [w for w in warehouse_column.split("_") if w]
    titled = []
    for index, word in enumerate(words):
        low = word.lower()
        titled.append(low if index and low in _SMALL_WORDS else low[:1].upper() + low[1:])
    return " ".join(titled)


def warehouse_name(display: str) -> str:
    """The inverse of `display_name`, lower-cased: "Day of Week" -> "day_of_week"."""
    return "_".join(display.split()).lower()


def inode_column_id(schema: str, relation: str, column: str) -> str:
    """`inode-<table inode>/<COLUMN>`: how Sigma ids a warehouse-table column."""
    return f"inode-{base62('inode', schema, relation, length=22)}/{column.upper()}"


def table_path(source: str) -> list[str]:
    """"warehouse.fct_orders" -> ["ANALYTICS", "WAREHOUSE", "FCT_ORDERS"]."""
    parts = source.split(".")
    if len(parts) != 2 or not all(parts):
        raise ModelError(f"table source {source!r} is not schema.relation")
    if source != source.lower():
        # Snowflake folds unquoted identifiers to upper case, and the path is
        # read back lower-cased; a mixed-case relation would come back renamed.
        raise ModelError(f"table source {source!r} is not lower case")
    return [DATABASE] + [p.upper() for p in parts]


def source_of_path(path: list[str]) -> str:
    """["ANALYTICS", "WAREHOUSE", "FCT_ORDERS"] -> "warehouse.fct_orders"."""
    if len(path) not in (2, 3) or not all(isinstance(p, str) and p for p in path):
        raise ModelError(f"warehouse path {path!r} is not [database,] schema, table")
    return ".".join(p.lower() for p in path[-2:])


def document_url(kind: str, name: str, url_id: str) -> str:
    return f"{APP_URL}/{kind}/{slug(name)}-{url_id}"
