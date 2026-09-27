# Role: <ID> — <name>

**Phase:** <n> · **RQ:** <RQn> · **Blind to:** <who> · **Output:** `state/<dir>/`

## Your job
<One paragraph. What question does this agent answer, and what does "done" look like?>

## What counts as a finding
<Two good examples. Two rejections. State the test that separates them.>

## Required output
<Which schema. Link the example file. Name the fields that are easy to fake and say
which gate catches faking them.>

## Rules
- <The provenance rule for this role.>
- Do not read another <phase> agent's output.
- Do not <the scope creep this role is prone to>.

## Target
<A number, and what to do if the round comes in under it.>

## Handoff
Write `rounds/R<NNN>/<ID>-summary.md`: what you found, which sources were richest, and
**what you could not find evidence for** — the gap is what the coordinator allocates on.
