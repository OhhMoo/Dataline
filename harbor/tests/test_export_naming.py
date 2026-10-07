"""Harbor names for (task, arm) and the --task-ids selector grammar."""
from __future__ import annotations

import re

import pytest

import test_export_support  # noqa: F401  (puts harbor/ on sys.path)
from alkera_harbor import harness
from alkera_harbor.naming import (ArmRef, NamingError, check_unique, harbor_name,
                                  select, short_name)

#: harbor/constants.py ORG_NAME_PATTERN (0.23.0), verbatim.
ORG_NAME_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9][a-zA-Z0-9._-]*$"


def test_names():
    assert short_name("GT001-aov-halving", "decision") == "gt001-aov-halving__decision"
    assert harbor_name("GT001-aov-halving", "decision") == \
        "alkera/gt001-aov-halving__decision"
    ref = ArmRef("GT004-doubling-window", "no-ticket")
    assert ref.short_name == "gt004-doubling-window__no-ticket"
    assert ref.selector == "GT004-doubling-window:no-ticket"


@pytest.mark.parametrize("task, arm", [("GT001", ""), ("GT__1", "a"), ("GT1", "a__b"),
                                       ("GT1", "a b"), ("GT1", "..x")])
def test_bad_names(task, arm):
    with pytest.raises(NamingError):
        short_name(task, arm)


def test_collisions_are_refused():
    with pytest.raises(NamingError, match="both map"):
        check_unique([ArmRef("GT001-a", "x"), ArmRef("gt001-a", "x")])


AVAILABLE = [ArmRef("GT001-aov-halving", "default"), ArmRef("GT001-aov-halving", "decision"),
             ArmRef("GT004-doubling-window", "no-ticket"),
             ArmRef("GT004-doubling-window", "default")]


@pytest.mark.parametrize("selectors, expected", [
    (None, sorted(AVAILABLE)),
    (["GT001-aov-halving"], [AVAILABLE[1], AVAILABLE[0]]),
    (["gt001"], [AVAILABLE[1], AVAILABLE[0]]),
    (["GT001-aov-halving:decision"], [AVAILABLE[1]]),
    (["gt004-doubling-window__no-ticket"], [AVAILABLE[2]]),
    (["alkera/gt004-doubling-window__default"], [AVAILABLE[3]]),
    (["GT001-aov-halving:default,GT004-doubling-window:no-ticket"],
     [AVAILABLE[0], AVAILABLE[2]]),
])
def test_selectors(selectors, expected):
    assert select(AVAILABLE, selectors) == expected


@pytest.mark.parametrize("bad", ["GT999", "GT001-aov-halving:nope", "x__y"])
def test_unknown_selector_is_an_error(bad):
    with pytest.raises(NamingError, match="matches nothing"):
        select(AVAILABLE, [bad])


def test_every_real_arm_has_a_unique_valid_harbor_name():
    refs = [ArmRef(t, a) for t in harness.task_ids() for a in harness.arms_of(t)]
    assert len(refs) == 32, [r.selector for r in refs]          # GT 28 + BI 4
    check_unique(refs)
    names = [r.harbor_name for r in refs]
    assert all(re.match(ORG_NAME_PATTERN, n) and ".." not in n for n in names)
    assert all(n == n.lower() for n in names)
    assert "alkera/gt001-aov-halving__decision" in names


def test_listing_never_inherits_a_stale_arm(monkeypatch):
    # GT004's arms do not exist on GT005: a leaked ALKERA_TASK_VARIANT must not
    # break (or silently change) the listing of another task
    monkeypatch.setenv("ALKERA_TASK_VARIANT", "ticket-only")
    assert harness.arms_of("GT005-assertion-never-ran") == ["decision", "default"]
