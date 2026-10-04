"""A minimal, deterministic TOML writer for the shapes this adapter emits.

`tomllib` is read-only and the gateway venv carries no TOML writer, so this
emits exactly what task.toml / dataset.toml need: tables (in the order given),
arrays of tables, strings, ints, finite floats, bools, arrays of scalars and
inline tables. Every document it renders is parsed back with `tomllib` and
compared to the input by the caller (`dumps(..., check=True)`), so a writer bug
fails the export instead of shipping a file that means something else.
"""
from __future__ import annotations

import math
import re
import tomllib

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


class TomlError(ValueError):
    pass


def string(value: str) -> str:
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def key(name: str) -> str:
    return name if _BARE.match(name) else string(name)


def value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise TomlError(f"non-finite float {v!r}")
        text = repr(v)
        return text if any(c in text for c in ".eE") else text + ".0"
    if isinstance(v, str):
        return string(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(value(x) for x in v) + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        return "{ " + ", ".join(f"{key(k)} = {value(x)}" for k, x in v.items()) + " }"
    raise TomlError(f"unsupported TOML value {v!r} ({type(v).__name__})")


def _is_table_array(v) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def _kind(dotted: str, v, inline_keys: frozenset[str]) -> str:
    if dotted in inline_keys:
        return "scalar"
    if isinstance(v, dict):
        return "table"
    if _is_table_array(v):
        return "array"
    return "scalar"


def _emit_table(lines: list[str], path: list[str], table: dict,
                inline_keys: frozenset[str]) -> None:
    """Scalars first, then sub-tables, then arrays of tables (TOML order rules)."""
    kinds = {k: _kind(".".join(path + [k]), v, inline_keys) for k, v in table.items()}
    for k, v in table.items():
        if kinds[k] == "scalar":
            lines.append(f"{key(k)} = {value(v)}")
    for k, v in table.items():
        if kinds[k] == "table":
            dotted = path + [k]
            inner = {c: _kind(".".join(dotted + [c]), x, inline_keys)
                     for c, x in v.items()}
            if not v or "scalar" in inner.values():
                # a table with only sub-tables is implied by their headers
                lines.append("")
                lines.append("[" + ".".join(key(p) for p in dotted) + "]")
            _emit_table(lines, dotted, v, inline_keys)
    for k, v in table.items():
        if kinds[k] == "array":
            dotted = path + [k]
            for item in v:
                lines.append("")
                lines.append("[[" + ".".join(key(p) for p in dotted) + "]]")
                _emit_table(lines, dotted, item, inline_keys)


def dumps(document: dict, *, header: list[str] | None = None,
          inline: tuple[str, ...] = (), check: bool = True) -> str:
    """Render `document`. `inline` names dotted keys rendered as inline tables
    (e.g. an empty `env` that should read `env = {}`). `header` lines are
    emitted first, verbatim (comments)."""
    lines: list[str] = list(header or [])
    body: list[str] = []
    _emit_table(body, [], document, frozenset(inline))
    while body and body[0] == "":
        body.pop(0)
    lines.extend(body)
    text = "\n".join(lines).rstrip("\n") + "\n"
    if check:
        parsed = tomllib.loads(text)
        if parsed != _plain(document):
            raise TomlError("rendered TOML does not round-trip to its input")
    return text


def _plain(v):
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v
