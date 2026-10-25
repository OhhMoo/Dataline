"""Composable factory-feed defects and oracle synthesis."""

from .oracle_composer import compose_task
from .t0_pipeline_stressors import STRESSORS, apply_stressors

__all__ = ["compose_task", "STRESSORS", "apply_stressors"]
