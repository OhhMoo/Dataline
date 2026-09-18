"""pytest collection policy for tasks/harness.

These five are EXECUTED scripts, not pytest modules: they drive their own
main() (test_regrade.py builds a real fixture rollout; test_variants.py takes
--fast; test_blindness_refused.py sys.exit()s at module level), and their
test_* functions take fixtures as plain arguments, so under pytest they can
only error at setup for want of `tmp`. Run them directly:

    python test_regrade.py

Ignoring them here - rather than editing the scripts - is what lets
`pytest .` in this directory report honestly, instead of failing collection
on files pytest was never meant to collect. The deselection happens in the
hook because collect_ignore does not apply to paths named explicitly on the
command line.
"""
from pathlib import Path

_SCRIPT_TESTS = (
    "test_regrade.py",
    "test_variants.py",
    "test_blindness_refused.py",
    "test_ledger_harness.py",
    "test_native_container.py",
)

collect_ignore = list(_SCRIPT_TESTS)


def pytest_collection_modifyitems(items):
    items[:] = [i for i in items
                if Path(str(i.fspath)).name not in _SCRIPT_TESTS]
