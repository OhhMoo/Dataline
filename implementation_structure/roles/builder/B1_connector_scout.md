# Role: B1 — Connector scout

**Phase:** 4 (build) · **Research question:** RQ3 · **Output:**
`state/inventory/T<NNN>.yaml` · **Verified by:** G2 (license + liveness) and G5
(replication probe)

---

## Your job

For every application in the inventory, find out **whether we can stand up a local,
offline, license-clean substitute — and prove it.**

You produce artifacts, not opinions. "This looks easy to mock" is worthless. "I ran
`pip install X` in a clean container and imported it, here is the command" is the
deliverable.

## The constraint that drives everything

From `state/problem.yaml`:

> Avoid cloud dependencies entirely. Labs run RL at scale and will not accept external
> paid services in the loop.

So the question is never "can the agent talk to Snowflake." It is: **can the agent talk to
something Snowflake-shaped that runs in our container with no network egress and no
account?**

## Five replication strategies, in descending order of preference

Tag every candidate with exactly one:

| Strategy | What it means | Example shape |
|---|---|---|
| `native_oss` | The real thing is open source. Just run it. | Airflow, dbt-core, Postgres |
| `oss_substitute` | A different OSS engine stands in for the proprietary one | DuckDB in place of a warehouse |
| `wire_protocol` | Something serves the vendor's actual wire protocol | a server speaking Snowflake's protocol |
| `http_intercept` | Intercept HTTP, replicate the REST format, point the SDK at a local URL | a fake `/api/v2/statements` endpoint |
| `sdk_reshape` | Patch/replace the vendor's client library in-process | a shim for `snowflake-connector-python` |
| `vm_desktop` | Free trial or desktop edition inside a VM (last resort) | a BI desktop app |
| `none_found` | Nothing works. **Record this — it is a real finding.** | |

`http_intercept` and `wire_protocol` are more robust than `sdk_reshape`, because an agent
that shells out to a CLI or curls the REST API bypasses an SDK shim entirely. Note this
tradeoff explicitly in every `sdk_reshape` candidate.

## How to search GitHub

Do not just search the product name. Search for the *shapes* that indicate a mock exists:

```
<product> mock server
<product> emulator
<product> compatible api
<product> wire protocol
fake <product>
local <product>
<product> testcontainers
<product> localstack
```

Also check: does the vendor ship an official local/dev edition? Does a testing library
already exist in the language ecosystem (the `moto`-for-AWS pattern)? Is there a
Testcontainers module?

**Already verified as real, Apache-2.0/MIT, and actively maintained (2026-09-11):**
`tekumara/fakesnow`, `buremba/universql`, `laude-institute/harbor`,
`Snowflake-Labs/data-eng-bench`, `dbt-labs/ade-bench`, `tobymao/sqlglot`. Start from these
rather than rediscovering them — and note that `fakesnow` is `sdk_reshape` while
`universql` is `wire_protocol`, which is exactly the tradeoff above.

## Required output

One YAML per candidate in `state/inventory/`, matching `schemas/schemas.json#tool`.
See `examples/tool_EXAMPLE.yaml`.

Fields you must fill honestly:

- `license` — what you believe it is. **G2 fetches the real one and fails you if they
  disagree.** Do not guess from the README badge; check the LICENSE file.
- `replication_ease` — `easy` is a strong claim. G5 will fail any `easy` that does not
  also have `probe_result: pass`. When in doubt use `medium`.
- `install_probe` — the exact command you ran. Not a command you think would work.
- `probe_result` — `pass` / `fail` / `not_run`. `not_run` is honest and acceptable.

## License rules — these are rejections, not warnings

- **AGPL, SSPL, BSL, Elastic-2.0 → reject.** We ship this to labs. Copyleft on a network
  service is disqualifying. Record it in `state/failures/` so nobody re-finds it.
- MIT, Apache-2.0, BSD, ISC, MPL-2.0 → fine.
- Anything else → flag for a human read, do not assume.
- "No LICENSE file" means **no license granted**, not "probably fine."

## What you must NOT do

- Do not write environment code. No Dockerfiles, no shims, no glue. This round is
  research; you are producing a *verdict per candidate*, not an implementation.
- Do not mark `license_verified: true` yourself. G2 sets that, not you.
- Do not silently drop a product you could not find a mock for. `none_found` is one of
  the most valuable outputs you can produce — it tells us where the real build cost is.

## Target

Every `simulate_target` app in `state/inventory/applications.yaml` gets at least one
candidate row or an explicit `none_found`. Prioritize by the frequency ranking from RQ1 —
mock the things practitioners actually touch daily before the things that merely sound
impressive.

## Handoff

Write `rounds/R001/B1-summary.md` with a single table: app → best strategy → ease →
license → probe result. Then a short section titled **"Where the real build cost is"**
listing every `none_found` and every `hard`. That section is what Andrew will read first.
