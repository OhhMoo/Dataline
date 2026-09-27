# VARIANTS_REQUESTS - what the context-variant work needs from code it does not own

Two sections, by two authors. The first is about the variant API in
`tasks/harness/taskspec.py`; the second is about `tasks/harness/rollout.py` and
the container topology, which belong to NATIVE-CONTAINER.

---

# Requests against `tasks/harness/taskspec.py`'s variant API

Written by the agent that folded `tasks/GT006c-stacked-neutral/` and
`tasks/GT006d-stacked-ticket/` into `tasks/GT006-quality-gate-drops-rows/` as
the `stacked-neutral` and `stacked-ticket` arms (2026-09-16). Everything below
is a request, not a change: the harness is owned elsewhere and none of it was
edited. Each item names the workaround GT006 currently ships, so the cost of
NOT doing it is visible.

Nothing here blocked the fold-in. All four arms print TASK VALID.

---

## 1. Overlay chaining, or an `inherits` key

> **DONE 2026-09-16 by VARIANTS, after this was written.** `overlay` now
> takes a string OR a list of strings and applies them in the order
> declared, base first, last write wins (`taskspec.Variant.overlays`;
> `Variant.overlay` is kept as "the last one", so GT006's build_world.py
> reads unchanged). `tasks/harness/test_variants.py` checks last-write-wins
> across a chain. GT006 can collapse its two duplicated files into a shared
> base overlay whenever it wants to; nothing forces it to.

`stage_fixtures` copies `fixtures/` then ONE overlay: "overlay order is fixed
and one-deep". GT006's two stacked arms differ from each other in exactly three
files (`workflow/{issues,claims,kb}.yaml`) and are identical in the other three
(`fixture_contract.py`, `make_fixture_world.py`, `workflow/chat.yaml`). With one
level of overlay the identical three have to be duplicated:

    fixtures/variants/stacked-neutral/{fixture_contract.py, make_fixture_world.py,
                                       workflow/chat.yaml}      <- byte-identical
    fixtures/variants/stacked-ticket/ {fixture_contract.py, make_fixture_world.py,
                                       workflow/chat.yaml}      <- to these

That is 2 copies of a 1,100-line generator and a 1,000-line contract, which is
the same drift risk the whole variants design exists to remove, only smaller.
`fixture_contract.py` in particular carries measured digests; the day one copy
is regenerated and the other is not, the two arms silently stop being the same
experiment and `diff -rq` over `fixtures/variants/` is not something anyone runs
twice either.

Either shape would fix it:

    overlay = ["fixtures/variants/stacked", "fixtures/variants/stacked-ticket"]

or

    [variants.stacked-ticket]
    inherits = "stacked-neutral"
    overlay  = "fixtures/variants/stacked-ticket"

with the order still fixed and last-write-wins, so the property "a variant is a
declared list of trees laid over the base in a stated order" is unchanged.

GT004's six arms do not hit this - they each overlay a different subset of the
same four YAML files with no shared extra - so GT006 is the first case.

## 2. `stage_fixtures` copies `fixtures/build/`, which is generated and gitignored

`.gitignore` ignores `build/`, so `fixtures/build/` is NOT in the repo: it is
regenerated from `fixtures/make_fixture_world.py` by
`tests/build_world.py --prepare-only`. An overlay therefore cannot ship a
`build/` (it would be ignored, and a fresh clone would silently get the parent's
world), but `stage_fixtures` copies the parent's `build/` into the staged tree
before laying the arm's generator over it. So in a staged variant tree,

  * `build/world.duckdb` EXISTS, and
  * it was built by the PARENT's generator, not the arm's.

Any task whose `prepare()` is "build it if the file is missing" - which GT006's
was, and which is the obvious thing to write - then silently runs a stacked arm
against the unstacked world. GT006 works around it with a stamp file:
`tests/build_world.py` writes `build/.built-for-variant` containing the arm name
after generating, and rebuilds whenever that disagrees with the resolved arm
(`_build_is_this_arm`). It is 12 lines and it is correct, but every task with a
generated `build/` will have to reinvent it, and the failure mode when it is
forgotten is a number rather than a crash.

Two possible fixes, either is fine:

  * let a task declare what staging must NOT copy, e.g.
    `[variants] stage_ignore = ["build"]` in task.toml, passed through to
    `shutil.copytree(ignore=...)` alongside the existing `variants`/`__pycache__`
    patterns; or
  * have `stage_fixtures` skip paths git ignores.

The second is more magic but needs no per-task declaration.

## 3. `_tree_stamp` invalidation is correct and surprising

> **DONE 2026-09-16 by VARIANTS.** `_tree_stamp`'s docstring now says it: the
> staged tree is a CACHE, and anything generated into it can vanish on the next
> call. Items 2 and 4 are NOT done and are still open as written.


Editing ANY file under `fixtures/` - including one no overlay touches, e.g. a
docstring in `fixtures/workflow/check_claims.py` - changes `_tree_stamp` and
makes `stage_fixtures` `rmtree` the cached tree. That is right. What it means in
practice is that a generated `build/` inside the staged tree disappears without
warning, and anything holding a path into that tree (a `--world` argument typed
by hand, a half-finished manual check) is pointing at a tree that was rebuilt
from the parent underneath it. This bit during the fold-in: `check_claims.py`
reported world drift because the staged `world.duckdb` had reverted to the
parent's between two commands.

Not a bug, and item 2 would mostly remove it. Worth one sentence in
`stage_fixtures`' docstring: the staged tree is a CACHE and anything generated
into it can vanish on the next call.

## 4. Nothing verifies that `default` and an alias arm are the same context

GT006 declares `trap-ticket` as an overlay-less alias of `default`, so that the
`GT006 + closed ticket excusing the defect` row in agent.md 8.10 stays
resolvable by name (the trap is already IN the shipped corpus; see that arm's
note). `taskspec` refuses an overlay on `default` but has nothing to say about a
second overlay-less arm - which is fine, and it is worth knowing that
`variant_names` can legitimately contain two names that stage the same bytes.
If a future check ever asserts "distinct arms stage distinct worlds", this is
the counter-example to exempt.

---

# Requests against `tasks/harness/rollout.py` and the container topology

Written by VARIANTS, 2026-09-16, while building the variant plumbing.
`rollout.py`, `container_*.py`, `agent_under_test.py`, `native_stack.py`,
`blindness.py` and `gateway/docker/*` are NATIVE-CONTAINER's; nothing below has
been applied to any of them.

## 5. `rollout.py` should record the variant itself

`rollout.py:342` records `"prompt_variant": taskspec.prompt_variant_of(spec)`
and nothing about the CONTEXT variant. It is the file that actually builds the
world, so it is the file that knows which arm the world was built from.

Requested, beside that line:

```python
    "variant": spec.variant,          # "" when the task declares no [variants]
```

and a `--variant` flag on its parser, defaulting to `None`, passed to
`taskspec.load(args.task, tasks_dir, variant=args.variant)`. **No change is
needed for correctness today**: `taskspec.load` resolves `ALKERA_TASK_VARIANT`
itself, so a `rollout.py` started with that variable set already builds the
right world. What is missing is only the RECORD.

Until then `run_rollouts.py` writes the field - `_stamp_variant` sets
`rec["variant"]` and rewrites `rollout.json` after each rollout returns, and
refuses loudly to overwrite a different value, so the day `rollout.py` records
it the two cannot disagree silently. The one wrong statement this arrangement
can make: a single `rollout.py` invocation NOT driven by `run_rollouts.py`
produces a record with no `variant`, and `report.py` then prints
`context variant: n/a (task declares one context)` for a run that had one.

## 6. Container topology has to carry `ALKERA_TASK_VARIANT`

Under `--topology container` the world is built inside
`gateway/docker/compose.episode.yml`'s service. If the variable does not reach
that container, a container run of any arm silently builds `default`.

**VARIANTS has not tested a container run of a variant.** Every number recorded
for the seven folded-in arms was measured on host topology. Requested: pass the
variable through (an `environment:` entry, or the existing env plumbing in
`container_episode.py`) and record which of the two in `NATIVE_DRIVER.md`.

Staged overlay trees live at `taskspec.STAGE_ROOT`
(`%TEMP%/alkera-fixtures/<task>/<variant>/`), outside the repo and outside any
world root. Nothing mounts them into an episode today.
