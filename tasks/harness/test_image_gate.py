"""Refuse a missing or stale episode image before native-container up().

    python -m pytest test_image_gate.py -q

No Docker. inspect / head / changed_paths are injected so these pin
assert_image_current against the same currency rule as
test_image_was_built_from_current_head (gateway + world, not tasks/).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import container_episode  # noqa: E402
import episode  # noqa: E402

TOKEN = "stale_episode_image"
IMAGE = "alkera-episode:head"
HEAD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OLD = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _labels(revision: str) -> dict:
    return {"Labels": {"org.alkera.source.revision": revision}}


def test_missing_image_raises():
    with pytest.raises(episode.HarnessError) as caught:
        container_episode.assert_image_current(
            IMAGE, inspect=lambda img: None, head=lambda: HEAD)
    assert TOKEN in str(caught.value)


def test_revision_matching_head_returns_dict():
    result = container_episode.assert_image_current(
        IMAGE, inspect=lambda img: _labels(HEAD), head=lambda: HEAD)
    assert result == {"image": IMAGE, "revision": HEAD, "head": HEAD}


def test_revision_behind_with_gateway_drift_raises():
    with pytest.raises(episode.HarnessError) as caught:
        container_episode.assert_image_current(
            IMAGE,
            inspect=lambda img: _labels(OLD),
            head=lambda: HEAD,
            changed_paths=lambda old, new: [
                "gateway/src/alkera_gateway/tools.py"],
        )
    assert TOKEN in str(caught.value)


def test_revision_behind_with_empty_changed_paths_is_current_enough():
    result = container_episode.assert_image_current(
        IMAGE,
        inspect=lambda img: _labels(OLD),
        head=lambda: HEAD,
        changed_paths=lambda old, new: [],
    )
    assert result == {"image": IMAGE, "revision": OLD, "head": HEAD}


def test_build_evidence_is_not_an_image_bearing_path():
    # Regression (2026-10-04): committing gateway/docker/evidence/image.json -
    # the RECORD of a build - made that very build stale, so every evidence
    # commit forced another rebuild, whose record forced another. Evidence
    # describes images; it is not an input to them.
    head = "6dbfb0b6f1bccbfdc710b121acf1656569fb6885"     # an evidence-only commit
    parent = "674a3a305380706733cc507655fe66a7273979e5"
    assert container_episode._image_scope_changed_paths(parent, head) == []
