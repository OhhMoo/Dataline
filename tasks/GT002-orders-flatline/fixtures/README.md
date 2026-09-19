# GT002 fixtures tree (the overlay base)

`tasks/harness/taskspec.stage_fixtures` reads this directory as the base of
every arm and copies `variants/<arm>/` over it for an arm that declares an
`overlay`. GT002's world is GENERATED, not copied: `tests/build_world.py`
deploys the cached warehouse from `tests/fixtures/build/` and writes the
workspace from `tests/fixtures/workspace_files.py`, so the control arm needs
nothing from here and this base tree is deliberately empty apart from this
file.

What the builder looks for in the staged tree:

    decision_arm.py     present only after the `decision` overlay is applied.
                        `stage(root, grading)` turns the freshly built control
                        world into the decision arm's world (DESIGN.md section 2).
    workflow/*.yaml     the arm's ticket / chat / wiki corpus, resolved by
                        world/world/workflow.py against the world clock.

`variants/` is never copied to an arm that does not name it, so `default`
reads exactly the shipped context.
