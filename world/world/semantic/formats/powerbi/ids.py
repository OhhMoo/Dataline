"""Deterministic identifiers for everything Power BI would mint at random.

A PBIP project written twice from the same model must be the same bytes (the
workspace is fingerprinted), yet Power BI Desktop fills it with GUIDs
(`logicalId`, `lineageTag`, relationship names) and 20-hex-character names
(pages, visuals, filters). Each one is therefore derived from the names it
belongs to: a GUID is uuid5 under one fixed namespace, a 20-character name is
the head of a SHA-1. They look like Desktop's and never change between builds.
"""
from __future__ import annotations

import hashlib
import uuid

#: Fixed so every build derives the same GUIDs; its value is arbitrary.
NAMESPACE = uuid.UUID("7b0c1f5e-3d2a-5c4b-9e8f-0a1b2c3d4e5f")


def guid(*parts: str) -> str:
    """A GUID derived from `parts` (logicalId, lineageTag, relationship name)."""
    return str(uuid.uuid5(NAMESPACE, "\x1f".join(parts)))


def short_id(*parts: str) -> str:
    """A 20-hex-character name, the shape PBIR gives pages, visuals and filters."""
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]
