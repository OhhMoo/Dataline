# DR-0007 — The copybook is the interface contract

Status: ACCEPTED
Raised: CR-2024-091
Superseded by: none

## Background

The nightly claims extract is a fixed-block file with no record delimiters.
Everything the distributed side knows about it comes from `copybooks/CLAIMS.CPY`:
field order, field widths, the code page, and the fact that `CLAIM-AMT` is
packed decimal with two implied places.

Three times between 2022 and 2024 the extract changed on the host and the
distributed loader was told afterwards. Each time the failure was silent for
at least one night, because a fixed-block reader given the wrong record length
does not stop — it produces rows.

## Decision

1. `copybooks/CLAIMS.CPY` is held in this repository and is the contract. A
   change to the extract is a change to that file, raised as a CR against both
   sides, and neither side deploys before the other has merged.
2. The record length is not to be written as a literal anywhere except the
   copybook and `conf/claims.cfg` `[source] record_length`. The loader takes
   it from its own module constant and `run.sh verify` takes it from the
   config; those two are to be reconciled — that reconciliation has not been
   done and is carried as a known defect.
3. A partition whose byte length is not a multiple of the record length is
   rejected, not partially loaded. `run.sh verify` performs that check before
   the load step; `loader/load_extract.py` performs it again.

## Consequences

- A host-side change without a matching CR fails loudly on the first night
  rather than producing plausible wrong numbers.
- The duplicate record length in point 2 is a real risk: an operator who
  changes `conf/claims.cfg` and not the loader gets a `verify` that passes and
  a load that raises, which is the safe direction, but the reverse is not
  safe. Raising this as a CR has been deferred twice.
