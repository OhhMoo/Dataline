"""Hawkes shocks: operational faults that cascade, seeded, compiled to the catalogue.

The fault layer (faults.py) makes a world misbehave at a declared, independent
rate per occurrence. Real incidents are not independent: a late delivery is
followed by a retry storm, an overrun by the next night's overrun. This module
draws the shocks of a world from a discrete-time multivariate Hawkes process
over the scheduler's own occurrence lattice, so a single number - the
branching ratio rho - dials a world from isolated faults (rho = 0) to near-
critical cascades (rho -> 1).

Three rules, inherited from faults.py and kept:

**1. Every random choice is a keyed draw.** `faults.draw(fault_seed, id, key)`,
keyed on the occurrence, never a PRNG stream. Whether kind k fires at an
occurrence is `draw(seed, "hawkes:<kind>", key)`; which earlier shock (or the
base rate) it descends from is `draw(seed, "hawkes:<kind>:parent", key)`.

**2. No new execution path.** The output is a ledger, `shock_schedule.json`,
and a list of `live.faults` entries that name their exact occurrence with
`only:`. `scheduler.advance` executes them through the code that already
exists; nothing here touches the warehouse, the landing zone or a run record.

**3. One shock per occurrence.** faults.py applies at most one fault to an
occurrence, the first in catalogue order. So does the ledger: when two kinds
fire on one occurrence, the first in spec order is the shock, the others are
recorded `suppressed: true` and never reach the catalogue. A suppressed shock
did not happen in the world, so it excites nothing and parents nothing.

## The process

Occurrences are the scheduler's dry-run plan (`scheduler.plan` over a copy of
live.json with the fault catalogue stripped): arrivals keyed `day:{i}` at their
base slot, job occurrences keyed `job@vts`. Kind k applies to the occurrences
of its target. In lattice order (vts, arrivals before jobs at one instant):

    lambda_k(o) = mu_k + sum_{j} sum_{shocks s of kind j before o}
                  alpha_kj * exp(-beta * (t(o) - t(s)))            [per occurrence]
    fire  iff  draw(seed, "hawkes:<kind>", key(o)) < 1 - exp(-lambda_k(o))

The parent of a fired shock is chosen by a second keyed draw, proportional to
each term of lambda_k(o): the base term makes it an immigrant (generation 0),
an earlier shock's term makes it that shock's child (its generation + 1). This
is the branching (cluster) representation, so every cascade is a tree in the
ledger.

## Calibrating alpha on a lattice (where the plan's formula is replaced)

The continuous-time recipe alpha = rho * beta * A / sr(A) gives a branching
ratio of rho only when the intensity is per HOUR and every instant is an
occurrence. Here lambda is per OCCURRENCE and occurrences are sparse (once a
day per arrival or job), so a shock of kind j produces, in expectation,

    G_kj = alpha_kj * S_kj,   S_kj = mean over j-occurrences o' of
                                     sum_{k-occurrences o after o'} exp(-beta (t(o) - t(o')))

children of kind k. On revenue_daily with beta = 0.05/h, S ~ 0.43 rather
than 1/beta = 20, so the continuous recipe would land a world at rho ~ 0.013
when rho = 0.6 was asked for. We set

    alpha_kj = rho * Ahat_kj / S_kj,   Ahat = A / sr(A),

so G = rho * Ahat and sr(G) = rho exactly. S is measured on the lattice
itself, over a tail long enough (exp(-beta * tail) = 1e-6) that the end of the
horizon does not truncate it; `sample` enumerates that tail but only shocks
inside the horizon.

Two biases remain, both downward and both measured in the tests: the firing
probability is 1 - exp(-lambda) <= lambda (offspring are attributed
proportionally to lambda's terms, so the expected offspring of a shock is
scaled by (1 - exp(-lambda))/lambda, ~ 1 - lambda/2), and the process starts
empty, so the first ~1/(beta (1 - rho)) hours are below stationary intensity.
On a sparse lattice the kernel lands on few occurrences, so lambda spikes and
the first bias is not small. `link: linear` (optional, default `exp`) fires
with probability min(lambda, 1) instead, which makes a shock's expected
offspring exactly sum(alpha * kernel) while lambda <= 1. Neither link removes
the third bias, which is the world's own rule 3: a child that collides with
another shock on its occurrence is suppressed and is lost to the count, with
its whole subtree.

## Excitation structure

`excitation: registry` derives A from declared state: each kind's footprint is
its target plus everything the theme manifest says is downstream of it - an
arrival's consumers (`params.missed_by`, else the jobs with no `after`), a
job's `after`-descendants, and every `exec.tables` those jobs write.
A_kj = Jaccard(footprint_k, footprint_j), diagonal 1. A kind may also name a
stressor from `tasks/stressors/registry.yaml` (`stressor: <name>`), whose
declared `state.tables`, `derived_tables` and `state.checkpoints` are folded
into its footprint - opt-in per kind, never matched by name, because the
shipped registry describes other worlds' tables.

An explicit square matrix (`excitation: [[...]]`, or `A: [[...]]`) in kinds
order is used as given.
"""
import copy
import math
from datetime import timedelta
from pathlib import Path

import yaml

from . import faults as faultlib
from . import scheduler
from .hashing import json_canon

SCHEDULE = Path("var/world/shock_schedule.json")
ID_PREFIX = "hawkes:"
KERNEL_EPS = 1e-6          # exp(-beta * tail): where the kernel's tail is cut
REPO = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO / "tasks" / "stressors" / "registry.yaml"


# --------------------------------------------------------------------------
# the spec
# --------------------------------------------------------------------------

def _matrix(M, n: int, what: str) -> list:
    if (not isinstance(M, (list, tuple)) or len(M) != n
            or any(not isinstance(r, (list, tuple)) or len(r) != n for r in M)):
        raise ValueError(f"{what} must be a {n}x{n} matrix in kinds order")
    out = [[float(x) for x in row] for row in M]
    if any(x < 0 or math.isnan(x) or math.isinf(x) for row in out for x in row):
        raise ValueError(f"{what} entries must be finite and non-negative")
    return out


def _target_token(target: dict) -> str:
    (cls, name), = target.items()
    return f"{cls}:{name}"


def validate(spec: dict) -> dict:
    """A normalized deep copy of `spec`, or ValueError naming what is wrong."""
    if not isinstance(spec, dict):
        raise ValueError("shock spec must be a mapping")
    spec = copy.deepcopy(spec)
    if "fault_seed" not in spec:
        raise ValueError("shock spec is missing fault_seed")
    try:
        rho = float(spec.get("rho"))
    except (TypeError, ValueError):
        raise ValueError(f"rho must be a number in [0, 1), got {spec.get('rho')!r}")
    if not 0.0 <= rho < 1.0:
        raise ValueError(f"rho must be in [0, 1) (rho >= 1 is a supercritical, "
                         f"explosive cascade), got rho={rho}")
    spec["rho"] = rho
    beta = float(spec.get("beta_per_hour", 0.0))
    if not beta > 0.0:
        raise ValueError(f"beta_per_hour must be > 0, got {beta}")
    spec["beta_per_hour"] = beta
    if "horizon_hours" in spec:
        spec["horizon_hours"] = float(spec["horizon_hours"])
        if not spec["horizon_hours"] > 0:
            raise ValueError("horizon_hours must be > 0")
    spec["link"] = spec.get("link", "exp")
    if spec["link"] not in ("exp", "linear"):
        raise ValueError(f"link must be 'exp' or 'linear', got {spec['link']!r}")
    kinds = spec.get("kinds")
    if not kinds:
        raise ValueError("shock spec declares no kinds")
    seen, arrival_kinds = set(), set()
    for entry in kinds:
        kind = entry.get("kind")
        if kind not in faultlib.KINDS:
            raise ValueError(f"unknown shock kind {kind!r} "
                             f"(known: {', '.join(sorted(faultlib.KINDS))})")
        target = entry.get("target")
        want = "arrival" if kind in faultlib.ARRIVAL_KINDS else "job"
        if (not isinstance(target, dict) or list(target) != [want]
                or not isinstance(target[want], str)):
            raise ValueError(f"{kind} needs target {{{want}: <name>}}, got {target!r}")
        if (kind, _target_token(target)) in seen:
            raise ValueError(f"{kind} on {_target_token(target)} is declared twice")
        seen.add((kind, _target_token(target)))
        if want == "arrival":
            # arrival keys are day:{i} whatever the stream, so one kind on two
            # streams would share its draws
            if kind in arrival_kinds:
                raise ValueError(f"{kind} is declared on two arrival targets")
            arrival_kinds.add(kind)
        entry["mu"] = float(entry.get("mu", 0.0))
        if entry["mu"] < 0:
            raise ValueError(f"{kind} mu must be >= 0")
        entry["params"] = dict(entry.get("params") or {})
    exc = spec.get("excitation", "registry" if "A" not in spec else None)
    if exc == "registry":
        spec["excitation"] = "registry"
    else:
        matrix = exc if isinstance(exc, (list, tuple)) else spec.get("A")
        if matrix is None:
            raise ValueError("excitation must be 'registry' or a square matrix")
        spec["excitation"] = _matrix(matrix, len(kinds), "excitation matrix A")
        spec.pop("A", None)
    return spec


def load_spec(path: Path) -> dict:
    """Read and validate a `shock_spec.yaml`."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return validate(raw)


# --------------------------------------------------------------------------
# the lattice
# --------------------------------------------------------------------------

def occurrences(root: Path, horizon_hours: float) -> list[dict]:
    """Every occurrence in (vnow, vnow + horizon], in lattice order.

    A dry run of `scheduler.plan` over live.json with the fault catalogue and
    its deferrals stripped: arrivals at their base slot, jobs at theirs. Runs
    nothing and writes nothing.
    """
    root = Path(root)
    state = scheduler.load(root)
    live = scheduler.load_live(root)
    if live is None:
        raise ValueError(f"{root} has no live-world control plane "
                         "(var/world/live.json); it has no occurrences")
    dry = {k: v for k, v in live.items()
           if k not in ("faults", "deferred_arrivals", "deferred_runs")}
    start = scheduler.parse_vtime(state["vnow"])
    events = scheduler.plan(state, dry, start,
                            start + timedelta(hours=float(horizon_hours)))
    out = []
    for e in events:
        vts = scheduler.iso(e["vts"])
        if e["kind"] == "arrival":
            out.append({"key": faultlib.arrival_key(e["day_index"]),
                        "cls": "arrival", "target": {"arrival": e["name"]},
                        "vts": vts})
        elif e["kind"] == "job":
            out.append({"key": faultlib.job_key(e["name"], vts), "cls": "job",
                        "target": {"job": e["name"]}, "vts": vts})
    return out


def _footprints(spec: dict, live: dict) -> None:
    """Attach each kind's declared state (`state`) from the theme manifest."""
    jobs = live.get("jobs") or {}
    children = {}
    for name in sorted(jobs):
        after = jobs[name].get("after")
        if after:
            children.setdefault(after["job"], []).append(name)

    def job_fp(name):
        if name not in jobs:
            raise ValueError(f"shock target job {name!r} is not a job of this "
                             f"world (jobs: {', '.join(sorted(jobs))})")
        out, todo = set(), [name]
        while todo:
            j = todo.pop()
            if f"job:{j}" in out:
                continue
            out.add(f"job:{j}")
            out |= {f"table:{t}" for t in
                    (jobs[j].get("exec") or {}).get("tables") or []}
            todo += children.get(j, [])
        return out

    arrival = (live.get("arrival") or {}).get("name")
    for entry in spec["kinds"]:
        target = entry["target"]
        if "job" in target:
            fp = job_fp(target["job"])
        else:
            if target["arrival"] != arrival:
                raise ValueError(f"shock target arrival {target['arrival']!r} "
                                 f"is not this world's arrival ({arrival!r})")
            consumers = (entry["params"].get("missed_by")
                         or [n for n in sorted(jobs) if not jobs[n].get("after")])
            fp = {f"arrival:{arrival}"}
            for c in consumers:
                fp |= job_fp(c)
        entry["state"] = sorted(fp)


# --------------------------------------------------------------------------
# excitation
# --------------------------------------------------------------------------

def spectral_radius(M: list[list[float]]) -> float:
    """Perron root of a non-negative square matrix, by power iteration.

    Iterates on M + I: same Perron vector, eigenvalue shifted by one, and the
    shift makes the dominant eigenvalue unique in modulus, so a periodic M
    ([[0, 1], [1, 0]]) converges instead of oscillating.
    """
    n = len(M)
    if n == 0:
        return 0.0
    M = _matrix(M, n, "M")
    v = [1.0 / n] * n
    norm = prev = 0.0
    for i in range(100_000):
        w = [sum(M[r][c] * v[c] for c in range(n)) + v[r] for r in range(n)]
        norm = max(w)
        v = [x / norm for x in w]
        if i > 20 and abs(norm - prev) <= 1e-15 * norm:
            break
        prev = norm
    return max(0.0, norm - 1.0)


def _registry_state(registry_path: Path | None, name: str) -> set:
    path = Path(registry_path) if registry_path else REGISTRY_PATH
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for entry in doc.get("stressors") or []:
        if entry.get("name") == name:
            st = entry.get("state") or {}
            return ({f"table:{t}" for t in (st.get("tables") or [])}
                    | {f"table:{t}" for t in (entry.get("derived_tables") or [])}
                    | {f"checkpoint:{c}" for c in (st.get("checkpoints") or [])})
    raise ValueError(f"stressor {name!r} is not in {path}")


def excitation_matrix(spec: dict, registry_path: Path | None) -> list[list[float]]:
    """A, in kinds order: given explicitly, or the Jaccard overlap of state."""
    kinds = spec["kinds"]
    exc = spec.get("excitation", "registry" if "A" not in spec else None)
    if exc != "registry":
        return _matrix(exc if isinstance(exc, (list, tuple)) else spec["A"],
                       len(kinds), "excitation matrix A")
    states = []
    for entry in kinds:
        st = set(entry.get("state") or [_target_token(entry["target"])])
        if entry.get("stressor"):
            st |= _registry_state(registry_path, entry["stressor"])
        states.append(st)
    n = len(kinds)
    return [[1.0 if k == j else
             len(states[k] & states[j]) / len(states[k] | states[j])
             for j in range(n)] for k in range(n)]


def _kernel_mass(lattice: list, times: list, kinds: list, beta: float) -> list:
    """S_kj: expected kernel mass a j-shock puts on later k-occurrences.

    Averaged over j-occurrences whose tail is complete (at least `tail` hours
    before the lattice ends), falling back to every j-occurrence on a lattice
    too short to have one.
    """
    n, K = len(lattice), len(kinds)
    match = [[o["target"] == kd["target"] for o in lattice] for kd in kinds]
    after = [[0.0] * n for _ in range(K)]      # sum over m > i of k-occurrences
    for k in range(K):
        for i in range(n - 2, -1, -1):
            decay = math.exp(-beta * (times[i + 1] - times[i]))
            after[k][i] = decay * (after[k][i + 1] + (1.0 if match[k][i + 1] else 0.0))
    tail = -math.log(KERNEL_EPS) / beta
    S = [[0.0] * K for _ in range(K)]
    for j in range(K):
        idx = [i for i in range(n) if match[j][i]]
        full = [i for i in idx if times[i] <= times[-1] - tail] or idx
        for k in range(K):
            if full:
                S[k][j] = sum(after[k][i] for i in full) / len(full)
    return S


def _alpha(rho: float, A: list, S: list) -> list:
    sr = spectral_radius(A)
    K = len(A)
    if sr == 0.0 or rho == 0.0:
        return [[0.0] * K for _ in range(K)]
    return [[rho * A[k][j] / sr / S[k][j] if S[k][j] > 0 else 0.0
             for j in range(K)] for k in range(K)]


# --------------------------------------------------------------------------
# sampling
# --------------------------------------------------------------------------

def simulate(lattice: list[dict], spec: dict, A: list[list[float]] | None = None,
             until: str | None = None) -> dict:
    """The shock schedule of `spec` over an explicit occurrence lattice.

    `lattice` is `occurrences`-shaped. Shocks are drawn only at occurrences
    with vts <= `until` (all of them when None); the rest of the lattice is
    used to measure the kernel mass, so a lattice that extends past the
    horizon calibrates alpha without edge truncation.
    """
    spec = validate(spec)
    kinds, beta, rho = spec["kinds"], spec["beta_per_hour"], spec["rho"]
    seed = spec["fault_seed"]
    linear = spec["link"] == "linear"
    if A is None:
        A = excitation_matrix(spec, None)
    A = _matrix(A, len(kinds), "excitation matrix A")
    lattice = sorted(lattice, key=lambda o: (o["vts"], o["cls"] == "job", o["key"]))
    if lattice:
        t0 = scheduler.parse_vtime(lattice[0]["vts"])
        times = [(scheduler.parse_vtime(o["vts"]) - t0).total_seconds() / 3600.0
                 for o in lattice]
    else:
        times = []
    alpha = _alpha(rho, A, _kernel_mass(lattice, times, kinds, beta)) if lattice \
        else [[0.0] * len(kinds) for _ in kinds]

    R = [0.0] * len(kinds)             # decayed count of executed shocks, by kind
    executed = []                      # (n, kind index, time, generation)
    shocks, t_prev = [], None
    for o, t in zip(lattice, times):
        if until is not None and o["vts"] > until:
            break
        if t_prev is not None:
            decay = math.exp(-beta * (t - t_prev))
            R = [r * decay for r in R]
        t_prev = t
        fired = []
        for k, entry in enumerate(kinds):
            if o["target"] != entry["target"]:
                continue
            lam = entry["mu"] + sum(alpha[k][j] * R[j] for j in range(len(kinds)))
            u = faultlib.draw(seed, f"{ID_PREFIX}{entry['kind']}", o["key"])
            if u < (min(lam, 1.0) if linear else 1.0 - math.exp(-lam)):
                fired.append(k)
        first = None
        for rank, k in enumerate(fired):
            entry = kinds[k]
            terms = [(None, 0, entry["mu"])]
            for n_s, j, t_s, gen in executed:
                if alpha[k][j] > 0.0:
                    terms.append((n_s, gen + 1,
                                  alpha[k][j] * math.exp(-beta * (t - t_s))))
            total = sum(w for _, _, w in terms)
            pick = faultlib.draw(seed, f"{ID_PREFIX}{entry['kind']}:parent",
                                 o["key"]) * total
            parent, generation = terms[-1][0], terms[-1][1]
            acc = 0.0
            for n_s, gen, w in terms:
                acc += w
                if pick < acc:
                    parent, generation = n_s, gen
                    break
            n = len(shocks)
            shocks.append({"n": n, "id": f"{ID_PREFIX}{entry['kind']}:{n}",
                           "kind": entry["kind"], "key": o["key"],
                           "target": dict(entry["target"]), "vts": o["vts"],
                           "parent": parent, "generation": generation,
                           "suppressed": rank > 0,
                           "params": dict(entry["params"])})
            if rank == 0:
                first = (n, k, t, generation)
        if first is not None:
            executed.append(first)
            R[first[1]] += 1.0

    live = [s for s in shocks if not s["suppressed"]]
    rho_emp = (sum(1 for s in live if s["parent"] is not None) / len(live)
               if live else None)
    return {"fault_seed": seed, "rho_target": rho, "rho_empirical": rho_emp,
            "beta_per_hour": beta, "A": A, "alpha": alpha, "shocks": shocks}


def sample(root: Path, spec: dict, registry_path: Path | None = None) -> dict:
    """The shock schedule for this world over [vnow, vnow + horizon_hours]."""
    root = Path(root)
    spec = validate(spec)
    if "horizon_hours" not in spec:
        raise ValueError("shock spec is missing horizon_hours")
    live = scheduler.load_live(root)
    if live is None:
        raise ValueError(f"{root} has no live-world control plane")
    _footprints(spec, live)
    A = excitation_matrix(spec, registry_path)
    tail = -math.log(KERNEL_EPS) / spec["beta_per_hour"]
    vnow = scheduler.parse_vtime(scheduler.load(root)["vnow"])
    until = scheduler.iso(vnow + timedelta(hours=spec["horizon_hours"]))
    lattice = occurrences(root, spec["horizon_hours"] + tail)
    return simulate(lattice, spec, A, until=until)


# --------------------------------------------------------------------------
# compile and install
# --------------------------------------------------------------------------

def to_catalogue(schedule: dict) -> list[dict]:
    """`live.faults` entries: one per executed shock, pinned with `only:`."""
    out = []
    for s in schedule["shocks"]:
        if s.get("suppressed"):
            continue
        entry = {"id": s["id"], "kind": s["kind"], "enabled": True,
                 "only": [s["key"]], "params": dict(s.get("params") or {})}
        if "job" in s["target"]:
            entry["jobs"] = [s["target"]["job"]]
        out.append(entry)
    return out


def install(root: Path, schedule: dict) -> None:
    """Write the shocks into live.json's catalogue and the ledger beside it.

    Shock entries go FIRST, so on an occurrence a theme fault also claims, the
    scheduled shock is the one that executes (faults.py: first match wins) and
    the ledger stays true. Any earlier `hawkes:` entries are replaced, so
    installing twice is installing once. A world whose theme declared no
    faults gets the bookkeeping `initial_live` would have given it.
    """
    root = Path(root)
    live = scheduler.load_live(root)
    if live is None:
        raise ValueError(f"{root} has no live-world control plane")
    entries = faultlib.catalogue(to_catalogue(schedule))
    keep = [f for f in faultlib.declared(live)
            if not str(f["id"]).startswith(ID_PREFIX)]
    live["faults"] = entries + keep
    live.setdefault("fault_seed", schedule["fault_seed"])
    live.setdefault("days_scheduled", live.get("days_landed", 0))
    live.setdefault("deferred_arrivals", {})
    live.setdefault("deferred_runs", {})
    live.setdefault("fault_log", [])
    scheduler.save_live(root, live)
    path = root / SCHEDULE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_canon(schedule) + b"\n")
