"""Reading and writing XML the way Tableau Desktop does.

WHY A HAND-WRITTEN SERIALIZER. `.twb` / `.tds` files are fingerprinted workspace
content and they are read by people: an engineer diffing a workbook in git sees
Tableau's own house style, and anything else is a tell. Desktop writes
single-quoted attributes in alphabetical order, `&apos;`/`&quot;` entities,
two-space indentation, a space before `/>`, the `<?xml version='1.0'
encoding='utf-8' ?>` declaration followed by a blank line and a fixed-width
`<!-- build ... -->` comment (all visible in the MIT-licensed samples of
github.com/tableau/document-api-python test/assets and
github.com/tableau/server-client-python test/assets). ElementTree's own writer
does none of that, so this module serializes an ElementTree tree itself.
Output is a pure function of the tree: no clock, no randomness.

FEATURE-CONDITIONAL NAMES. Tableau prefixes elements and attributes introduced
by a feature with `_.fcp.<Feature>.true...` (the name a reader that knows the
feature uses) and writes a `_.fcp.<Feature>.false...` twin for readers that do
not (see `<document-format-change-manifest>`). `normalize` keeps the `true`
spelling under its plain name and drops the `false` twins, which is what any
current Tableau release does when it opens the file.

SAFETY. Agent-published files reach `parse` through the fake Tableau Server.
A DTD has no business in a Tableau file (none ever carries one), so any
`<!DOCTYPE`/`<!ENTITY` is refused before expat sees it.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from ...errors import ModelError

USER_NS = "http://www.tableausoftware.com/xml/user"
_USER = "{" + USER_NS + "}"

# Tableau Desktop 2025.1.2: a real release build string (seen in MIT-licensed
# workbooks saved by it). REST API 3.25 is the version paired with 2025.1
# (Tableau REST API "API and Tableau Server versions" table). Every 2023-2025
# file says version='18.1'; from 2024.3 the object-model tags are written
# without their `_.fcp.ObjectModel...` prefixes, which is what this writes.
PRODUCT_VERSION = "2025.1.2"
BUILD = "20251.25.0520.1026"
SOURCE_BUILD = f"{PRODUCT_VERSION} ({BUILD})"
FILE_VERSION = "18.1"

_FCP = re.compile(r"^_\.fcp\.([A-Za-z0-9]+)\.(true|false)\.\.\.(.*)$")


def parse(data: bytes, what: str) -> ET.Element:
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        raise ModelError(f"{what}: a Tableau file never carries a DTD; refusing to parse it")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ModelError(f"{what}: not well-formed XML ({exc})") from None
    return normalize(root)


def normalize(elem: ET.Element) -> ET.Element:
    """Resolve `_.fcp.` feature prefixes in place (module docstring)."""
    kept = []
    for child in list(elem):
        m = _FCP.match(child.tag) if isinstance(child.tag, str) else None
        if m and m.group(2) == "false":
            elem.remove(child)
            continue
        if m:
            child.tag = m.group(3)
        kept.append(child)
    for name in list(elem.attrib):
        m = _FCP.match(name)
        if m:
            value = elem.attrib.pop(name)
            if m.group(2) == "true":
                elem.attrib[m.group(3)] = value
    for child in kept:
        normalize(child)
    return elem


def fcp(feature: str, name: str, enabled: bool = True) -> str:
    return f"_.fcp.{feature}.{'true' if enabled else 'false'}...{name}"


# ----------------------------------------------------------------- writing
def _escape_attr(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("'", "&apos;").replace('"', "&quot;")
            .replace("\r", "&#13;").replace("\n", "&#10;").replace("\t", "&#9;"))


def _escape_text(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("'", "&apos;").replace('"', "&quot;").replace("\r", "&#13;"))


def _qname(name: str) -> str:
    return "user:" + name[len(_USER):] if name.startswith(_USER) else name


def _attr_key(name: str) -> str:
    # Desktop sorts attributes by their written name, prefixes included
    # (`_.fcp...` sorts before letters, `user:` among the u's).
    return _qname(name)


def _write(elem: ET.Element, depth: int, out: list[str]) -> None:
    pad = "  " * depth
    tag = _qname(elem.tag)
    attrs = "".join(f" {_attr_key(k)}='{_escape_attr(str(v))}'"
                    for k, v in sorted(elem.attrib.items(), key=lambda kv: _attr_key(kv[0])))
    children = list(elem)
    text = elem.text if elem.text and elem.text.strip() else None
    if not children and text is None:
        out.append(f"{pad}<{tag}{attrs} />")
    elif not children:
        out.append(f"{pad}<{tag}{attrs}>{_escape_text(text)}</{tag}>")
    else:
        out.append(f"{pad}<{tag}{attrs}>" + (_escape_text(text) if text else ""))
        for child in children:
            _write(child, depth + 1, out)
        out.append(f"{pad}</{tag}>")


def serialize(root: ET.Element, *, user_ns: bool = True) -> bytes:
    """A whole Tableau document: declaration, build comment, root."""
    if user_ns:
        root.set("xmlns:user", USER_NS)
    out = ["<?xml version='1.0' encoding='utf-8' ?>", "",
           f"<!-- build {BUILD:<49}-->"]
    _write(root, 0, out)
    return ("\n".join(out) + "\n").encode("utf-8")


def sub(parent: ET.Element, tag: str, attrib: dict | None = None, text: str | None = None,
        **kw) -> ET.Element:
    elem = ET.SubElement(parent, tag, {k: str(v) for k, v in (attrib or {}).items()},
                         **{k: str(v) for k, v in kw.items()})
    if text is not None:
        elem.text = text
    return elem


def user(name: str) -> str:
    """An attribute in Tableau's `user:` namespace (ui hints on filters)."""
    return _USER + name


def local(elem: ET.Element, name: str) -> str | None:
    """Read a `user:` attribute."""
    return elem.get(_USER + name)
