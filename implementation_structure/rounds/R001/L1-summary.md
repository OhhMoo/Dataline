# L1 — Data-engineering practitioner workflows

L1 produced 23 findings from 16 distinct URLs. The richest sources were first-party
incident reports, reproducible GitHub issues, Airflow Summit talks, and empirical studies
of pipeline defects. The recurring pattern was not a job that simply crashes: it was a
successful-looking pipeline that emitted wrong or incomplete state, followed by a risky
repair that could create a second incident. The strongest task seeds are non-idempotent
backfills, semantic and schema drift, cross-system orchestration diagnosis, lineage-based
SLA triage, CDC/WAL failures, and disaster recovery under destructive ambiguity.

The largest remaining evidence gaps are day-to-day access-control/governance work,
warehouse cost incidents grounded in first-party postmortems, and production examples
that cross a warehouse, transformation layer, orchestrator, and BI surface in one trace.
Several sources are vendor-authored and must retain their existing confidence discounts.

