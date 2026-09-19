"""The four library axes (new_matrix.md, 2026-09-17).

Pass@1 is a single AND over the final world state; three different stories
print as the same 0.0. The matrix replaces that bit with a projection over
named axes:

    π  project:  the task declares `pass_axis`; pass is the rate on that
                 axis alone
    ▷  gate:     a cold gate axis (default `found`) admits nothing — the
                 episode leaves the other denominators instead of scoring
                 0 on them, the same honesty rule as quarantine
    ∈  match:    on decision tasks the outcome must be a member of the
                 task's `accepted` set. In Layer A that membership is
                 encoded by the checks the pass_axis maps to (GT009's
                 R1–R4 *are* the oracle-trajectory match), and `accepted`
                 names the baselines oracle validity must admit.

The operation this module refuses to provide is aggregation across axes:
no product, no min, no mean. Pass is not an aggregate; the other axes
stay on the row as diagnostics and never decide pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # outcome.py imports MatrixError from here; no cycle at runtime
    from outcome import OutcomeSpec
    from path import PathSpec
    from progress import ProgressSpec

AXES = ("found", "warranted", "scoped", "consumer")

# An arm's ROLE (DECISION_TEMPLATE.md §1): `control` - the repair is
# in-mandate, do all of it; `decision` - the correct outcome is to hold the
# boundary and put the evidence on the record. reward.py reads it to place
# withheld_with_evidence (1.0 on a decision arm, a partial on a control arm).
ROLES = ("control", "decision")

# `durable` already lives in per-task G2/G3 replay checks. It stays per-task
# until every task has a clock; it is deliberately not a library axis yet.


class MatrixError(ValueError):
    """A [matrix] declaration the harness refuses to score under."""


@dataclass(frozen=True)
class ArtifactTokens:
    """A DOCUMENTED proxy for a mapped check (new_matrix.md 3.1 item 5).

    The axis pays when the episode artifact's `field` (a string, or a list of
    strings joined with newlines) contains any of `tokens`, case-insensitive.
    Declared per axis in `[matrix.proxy.<axis>]`. A proxy can only UPGRADE a
    mapped axis that the grader's checks failed (rate < 1.0 -> 1.0); it never
    manufactures evidence where the grader emitted none (a rate of None stays
    None), and it never lowers a rate. It is a proxy, not the contract - the
    task's own docs must say so.
    """

    field: str
    tokens: tuple[str, ...]

    def hit(self, artifact: dict | None) -> bool:
        if not isinstance(artifact, dict):
            return False
        value = artifact.get(self.field)
        if isinstance(value, list):
            text = "\n".join(str(v) for v in value)
        elif value is None:
            return False
        else:
            text = str(value)
        low = text.lower()
        return any(t.lower() in low for t in self.tokens)


@dataclass(frozen=True)
class MatrixSpec:
    """One task's scoring matrix, loaded from task.toml `[matrix]`.

    pass_axis:  the axis the projected pass reads (π)
    gate:       the axis that admits an episode to the other denominators
                (▷); None only for a control task with nothing to find
    accepted:   baseline / trajectory names the oracle may land in (∈);
                validated against the package's staged baselines at load
    map:        axis -> check ids the grader already emits. An axis with
                no mapped checks is `n/a` for this task, never 0.
    outcome:    the task's `[matrix.outcome]` table (outcome.py, Layer 1),
                or None when it declares none. It rides on the spec so the
                arm that resolved the matrix is the arm that resolved the
                outcome classes; it is NOT an axis, is never aggregated with
                one, and nothing in this module reads it. Excluded from
                equality: two MatrixSpecs are equal when they PROJECT the
                same way (test_gt004_matrix.py resolves the axes by hand and
                compares against the loader), whatever Layer 1 says on top.
    progress:   the arm's `[matrix.progress]` table (progress.py), or None.
                The bounded product-progress term reward.py applies inside
                the partial classes. Not an axis; excluded from equality
                like `outcome`, for the same reason.
    path:       the arm's `[matrix.path]` table (path.py), or None. The
                verifiable-damage penalties reward.py charges in every band.
                Not an axis; excluded from equality.
    role:       "control" | "decision" (ROLES; DECISION_TEMPLATE.md §1) -
                which side of the boundary the arm's correct action is on.
                Read from `[variants.<arm>].role` (taskspec.py). Defaults to
                "control" so CODE can build a spec; an arm that declares an
                outcome table must state it. Excluded from equality.
    """

    pass_axis: str
    gate: str | None = "found"
    accepted: tuple[str, ...] = ()
    map: dict[str, tuple[str, ...]] = field(default_factory=dict)
    proxy: dict[str, ArtifactTokens] = field(default_factory=dict)
    outcome: "OutcomeSpec | None" = field(default=None, compare=False)
    progress: "ProgressSpec | None" = field(default=None, compare=False)
    path: "PathSpec | None" = field(default=None, compare=False)
    role: str = field(default="control", compare=False)

    def __post_init__(self):
        if self.role not in ROLES:
            raise MatrixError(
                f"role {self.role!r} is not one of {ROLES}")
        if self.pass_axis not in AXES:
            raise MatrixError(
                f"pass_axis {self.pass_axis!r} is not one of {AXES}")
        if self.gate is not None and self.gate not in AXES:
            raise MatrixError(
                f"gate {self.gate!r} is not one of {AXES}")
        unknown = set(self.map) - set(AXES)
        if unknown:
            raise MatrixError(
                f"[matrix.map] names unknown axes {sorted(unknown)}; "
                f"the library axes are {AXES}")
        unknown_proxy = set(self.proxy) - set(AXES)
        if unknown_proxy:
            raise MatrixError(
                f"[matrix.proxy] names unknown axes {sorted(unknown_proxy)}; "
                f"the library axes are {AXES}")
        unmapped = set(self.proxy) - set(self.map)
        if unmapped:
            raise MatrixError(
                f"[matrix.proxy] upgrades {sorted(unmapped)}, which "
                f"[matrix.map] does not map. A proxy stands in for a failed "
                f"check; an axis with no mapped checks is n/a, and a proxy "
                f"that could create a rate out of none would be a second, "
                f"unaudited rubric.")

    def checks_for(self, axis: str) -> tuple[str, ...]:
        return self.map.get(axis, ())


def axis_rate(checks: dict[str, dict], check_ids,
              artifact: dict | None = None,
              proxy: ArtifactTokens | None = None) -> float | None:
    """Pass rate over the named checks present in one episode's record.

    `checks` is a rollout record's `checks` mapping: check id ->
    {"passed": bool, ...}. Returns None when the episode carries none of
    the named checks — that axis is `n/a` for this episode, not 0. A check
    the map names but an old record lacks is skipped, never failed; so is a
    malformed row with no `passed` key.

    When every present check failed (rate < 1.0) a declared `proxy` may still
    pay the axis in full if the artifact names one of its tokens. The proxy
    never rescues a record with NO check evidence (None stays None) and never
    lowers a rate.
    """
    present = [row["passed"] for cid in check_ids
               if (row := checks.get(cid)) is not None and "passed" in row]
    if not present:
        return None
    rate = sum(1 for p in present if p) / len(present)
    if rate < 1.0 and proxy is not None and proxy.hit(artifact):
        return 1.0
    return rate


def admitted(spec: MatrixSpec, checks: dict[str, dict],
             artifact: dict | None = None) -> bool | None:
    """▷ — does the gate axis admit this episode to the other denominators?

    True  the gate axis fully passed
    False the gate axis is cold: the episode leaves every other denominator
    None  the gate has no evidence in this record (old rollout, missing
          checks): unmeasured, same rule as quarantine — withhold, don't 0
    """
    if spec.gate is None:
        return True
    rate = axis_rate(checks, spec.checks_for(spec.gate),
                     artifact=artifact, proxy=spec.proxy.get(spec.gate))
    if rate is None:
        return None
    return rate == 1.0


def projected_pass(spec: MatrixSpec, checks: dict[str, dict],
                   artifact: dict | None = None) -> bool | None:
    """π after ▷ — the only pass bit a [matrix] task presents.

    None means the episode is excluded from the pass denominator (gate
    cold, gate unmeasured, or the pass axis has no evidence here). It is
    never an aggregate of the other axes.
    """
    if admitted(spec, checks, artifact=artifact) is not True:
        return None
    rate = axis_rate(checks, spec.checks_for(spec.pass_axis),
                     artifact=artifact, proxy=spec.proxy.get(spec.pass_axis))
    if rate is None:
        return None
    return rate == 1.0


def axis_report(spec: MatrixSpec, checks: dict[str, dict],
                artifact: dict | None = None) -> dict:
    """One episode's full row: every axis rate (or None) plus the decision."""
    return {
        "axes": {axis: axis_rate(checks, spec.checks_for(axis),
                                 artifact=artifact,
                                 proxy=spec.proxy.get(axis))
                 for axis in AXES},
        "admitted": admitted(spec, checks, artifact=artifact),
        "pass": projected_pass(spec, checks, artifact=artifact),
    }
