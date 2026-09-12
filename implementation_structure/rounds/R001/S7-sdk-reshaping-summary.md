# S7-sdk-reshaping — Phase 3 summary (RQ3 claims)

Derivation path: MOCK-ABILITY VIA SDK RESHAPING. Produced 5 claims, C013-C017, in
state/claims/. All status=proposed, rq=RQ3, proposed_by=S7-sdk-reshaping.

## What I derived

- **C013** — Position on the mandated tradeoff: fakesnow (sdk_reshape) > universql
  (wire_protocol). universql needs a live Snowflake account for catalog/access control and
  cannot read native Snowflake tables, violating E2 (no cloud at runtime); fakesnow is fully
  local, and universql itself is built on SQLGlot + fakesnow. Evidence: S316, S317.
- **C014** — Where in-process SDK patching is right vs breaks: right only when the task pins
  the agent to one language runtime; breaks when the agent shells out to a CLI or curls REST
  (shim lives in the patched process). fakesnow's standalone HTTP-server mode (works with
  JDBC and the Snowflake CLI) is the escape hatch — server mode should be the default.
  Evidence: S316.
- **C015** — Reshaped-SDK grading false positives: fakesnow is dialect-liberal with
  non-deterministic row ordering; sqlglot transpiles but does not validate. Mitigations:
  fully-specified ORDER BY in verifiers + data-eng-bench's dual-backend dialect-fidelity
  axis. Evidence: S316, S303, S318.
- **C016** — CLI/REST-surfaced apps (warehouse CLIs, BI tools) cannot be SDK-reshaped; they
  need HTTP/DNS-layer interception at a fake local URL (E3). Apps with no OSS mock server
  get deprioritized or substituted with native-OSS equivalents (DuckDB). Evidence: S316, S507.
- **C017** — Rollout economics are sdk_reshaping's decisive edge: fakesnow's per-connection
  isolated databases map onto parallel RL rollouts on Harbor; external-account clone-and-sweep
  (data-eng-bench retail_clone_* + sweeper) is a fidelity-check fallback, not the
  training-time substrate. Evidence: S316, S303.

## Richest evidence

S316 (fakesnow) carried nearly everything: both interception modes, the caveats, the
per-connection isolation detail, and the pre-verified Apache-2.0 verdict. S317 was the
cleanest kill-shot of the round (universql's own README disqualifies it under E2). S303's
dual-backend pattern is the strongest reusable design. S318 and S507 were solid supporting
cites.

## Least confident

**C016** — it generalizes from one strong instance (fakesnow server mode covering CLI/JDBC)
plus the E3 constraint to a fleet-wide rule about HTTP/DNS interception for BI and CLI
surfaces; the corpus has no per-app mock-server findings for Tableau/Looker/Power BI CLIs,
so the "deprioritize or substitute" recommendation rests on absence of evidence. **C014**'s
bypass claim (subprocess/curl escapes an in-process shim) is mechanism-level reasoning from
how patching works rather than a cited incident — S316 supports the fix (server mode) but
not the failure story directly.

## Notes for the coordinator

- C013 and C017 overlap on rollout economics but argue different halves (emulator choice vs
  training-time architecture); merge is possible if dedup flags them.
- Flag for RQ3 builder follow-up: need G5 install probes on fakesnow server mode and an
  inventory pass for OSS REST-mock servers for BI apps before C016's deprioritization list
  can be made concrete.
