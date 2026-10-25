"""Deterministic, clocked factory-feed hazards."""

from .arrival_file_hazards import ARRIVAL_HAZARDS, apply_arrival_hazards
from .temporal_contract_hazards import TEMPORAL_HAZARDS, apply_temporal_hazards

ALL_HAZARDS = {**ARRIVAL_HAZARDS, **TEMPORAL_HAZARDS}
