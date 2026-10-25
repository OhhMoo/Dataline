"""Deterministic 1,080-blueprint combinatorial task matrix and compatibility validator.

Spans the 4 orthogonal axes defined in plan_1017.md:
  1. 12 Enterprise World Themes (THEMES)
  2. 15 Composed (t0_stressor + R1..R4_hazard_pair) Topologies (FAULT_TOPOLOGIES)
  3. 3 Governance & Epistemic Twin Modes (GOVERNANCE_MODES)
  4. 2 Held-Out Schedule Horizons & Lexicon Seeds (SCHEDULE_SEEDS)

Total blueprints = 12 * 15 * 3 * 2 = 1,080 (360 paired twin groups of size 3).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parents[3]
    / "experiments"
    / "scale_1000"
    / "catalog_1080.jsonl"
)

VALID_CONTRACTS: tuple[str, ...] = (
    "exactly_once",
    "liveness",
    "version_precedence",
    "cutoff_integrity",
    "consumer_consistency",
    "auditability",
    "authorization",
)

# ---------------------------------------------------------------------------
# Axis 1: 12 Enterprise World Themes
# Ordered with settlement_clearing first so FT0001 matches the reference ID
# FT0001-settlement-clearing-ft01-auth-a.
# ---------------------------------------------------------------------------
THEMES: dict[str, dict[str, Any]] = {
    "settlement_clearing": {
        "domain_key": "card_settlement_clearing",
        "project_prefix": "PAY",
        "primary_fact_table": "warehouse.fct_settlement_daily",
        "cutoff_window_table": "warehouse.settlement_corrections",
        "handoff_table": "warehouse.clearing_bank_handoff",
        "raw_tables": ["raw.payment_events", "raw.merchant_schedule"],
        "pipeline_type": "python_duckdb",
        "landing_format": "csv",
        "kb_space_key": "PAY",
        "supports_bi": True,
    },
    "commerce_analytics": {
        "domain_key": "ecommerce_analytics",
        "project_prefix": "ANA",
        "primary_fact_table": "warehouse.fct_orders",
        "cutoff_window_table": "warehouse.close_window_control",
        "handoff_table": "warehouse.fct_order_lines",
        "raw_tables": ["raw.orders", "raw.order_lines", "raw.customers", "raw.products"],
        "pipeline_type": "dbt_duckdb",
        "landing_format": "csv",
        "kb_space_key": "AE",
        "supports_bi": True,
    },
    "revenue_daily": {
        "domain_key": "retail_revenue_recognition",
        "project_prefix": "REV",
        "primary_fact_table": "warehouse.fct_revenue",
        "cutoff_window_table": "warehouse.revenue_cutoff_ledger",
        "handoff_table": "warehouse.gl_revenue_extract",
        "raw_tables": ["raw.daily_revenue"],
        "pipeline_type": "dbt_duckdb",
        "landing_format": "csv",
        "kb_space_key": "DE",
        "supports_bi": True,
    },
    "sftp_vendor": {
        "domain_key": "sftp_vendor_integrations",
        "project_prefix": "VND",
        "primary_fact_table": "warehouse.fct_orders_daily",
        "cutoff_window_table": "warehouse.vendor_settlement_window",
        "handoff_table": "warehouse.partner_reconciliation_handoff",
        "raw_tables": ["raw.vendor_orders", "raw.vendor_returns"],
        "pipeline_type": "dbt_duckdb",
        "landing_format": "csv",
        "kb_space_key": "INT",
        "supports_bi": False,
    },
    "mainframe_batch": {
        "domain_key": "legacy_mainframe_claims_batch",
        "project_prefix": "MFB",
        "primary_fact_table": "warehouse.fct_claims_daily",
        "cutoff_window_table": "warehouse.batch_cutoff_control",
        "handoff_table": "warehouse.statutory_reserve_extract",
        "raw_tables": ["raw.claims_extract"],
        "pipeline_type": "dbt_duckdb",
        "landing_format": "fixed_width",
        "kb_space_key": "BATCH",
        "supports_bi": False,
    },
    "excel_close": {
        "domain_key": "financial_controller_close",
        "project_prefix": "FIN",
        "primary_fact_table": "warehouse.fct_gl_monthly",
        "cutoff_window_table": "warehouse.period_close_signoff",
        "handoff_table": "warehouse.consolidation_trial_balance",
        "raw_tables": ["raw.gl_lines", "raw.manual_adj"],
        "pipeline_type": "dbt_duckdb",
        "landing_format": "csv",
        "kb_space_key": "FIN",
        "supports_bi": True,
    },
    "saas_subscription_mrr": {
        "domain_key": "b2b_saas_arr_mrr",
        "project_prefix": "MRR",
        "primary_fact_table": "warehouse.fct_mrr_daily",
        "cutoff_window_table": "warehouse.revrec_close_window",
        "handoff_table": "warehouse.asc606_close_ledger",
        "raw_tables": ["raw.subscription_events", "raw.invoice_webhooks"],
        "pipeline_type": "python_duckdb",
        "landing_format": "jsonl",
        "kb_space_key": "REVOPS",
        "supports_bi": True,
    },
    "ad_attribution_events": {
        "domain_key": "adtech_conversion_attribution",
        "project_prefix": "ATT",
        "primary_fact_table": "warehouse.fct_attributed_conversions",
        "cutoff_window_table": "warehouse.billing_lock",
        "handoff_table": "warehouse.partner_payout_handoff",
        "raw_tables": ["raw.impressions", "raw.clicks", "raw.conversions"],
        "pipeline_type": "python_duckdb",
        "landing_format": "jsonl",
        "kb_space_key": "ADOPS",
        "supports_bi": True,
    },
    "supply_chain_inventory": {
        "domain_key": "multi_dc_inventory_ledger",
        "project_prefix": "INV",
        "primary_fact_table": "warehouse.fct_inventory_position",
        "cutoff_window_table": "warehouse.customs_cutoff",
        "handoff_table": "warehouse.3pl_settlement_extract",
        "raw_tables": ["raw.asn_receipts", "raw.dc_movements", "raw.cycle_counts"],
        "pipeline_type": "dbt_duckdb",
        "landing_format": "csv",
        "kb_space_key": "SCM",
        "supports_bi": False,
    },
    "healthcare_claims_edi": {
        "domain_key": "payer_edi_837_835_adjudication",
        "project_prefix": "CLM",
        "primary_fact_table": "warehouse.fct_adjudicated_claims",
        "cutoff_window_table": "warehouse.cms_submission_window",
        "handoff_table": "warehouse.actuarial_reserve_handoff",
        "raw_tables": ["raw.edi_837_claims", "raw.edi_835_remits", "raw.provider_roster"],
        "pipeline_type": "python_duckdb",
        "landing_format": "csv",
        "kb_space_key": "EDI",
        "supports_bi": False,
    },
    "fintech_kyc_risk_limits": {
        "domain_key": "merchant_risk_dispute_reserves",
        "project_prefix": "RSK",
        "primary_fact_table": "warehouse.fct_merchant_exposure",
        "cutoff_window_table": "warehouse.risk_committee_signoff",
        "handoff_table": "warehouse.treasury_reserve_handoff",
        "raw_tables": ["raw.dispute_events", "raw.kyc_reviews", "raw.payout_holds"],
        "pipeline_type": "python_duckdb",
        "landing_format": "jsonl",
        "kb_space_key": "RISK",
        "supports_bi": True,
    },
    "iot_telemetry_billing": {
        "domain_key": "iot_usage_metering_billing",
        "project_prefix": "IOT",
        "primary_fact_table": "warehouse.fct_billable_usage",
        "cutoff_window_table": "warehouse.billing_period_lock",
        "handoff_table": "warehouse.invoice_run_handoff",
        "raw_tables": ["raw.meter_heartbeats", "raw.usage_rollups", "raw.contract_commits"],
        "pipeline_type": "python_duckdb",
        "landing_format": "jsonl",
        "kb_space_key": "METER",
        "supports_bi": True,
    },
}

# ---------------------------------------------------------------------------
# Axis 2: 10 Stageable t0 Stressors & 14 Held-Out Future-Night (R1..R4) Hazards
# ---------------------------------------------------------------------------
T0_STRESSORS: dict[str, dict[str, Any]] = {
    "S01_checkpoint_calendar_advance_on_gap": {
        "description": "Advances high-water checkpoint calendar date even when partition is missing",
        "contracts": ["liveness", "auditability"],
        "supported_pipeline_types": ["python_duckdb", "dbt_duckdb"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "S02_checkpoint_filter_blocks_backfill": {
        "description": "Strict day > checkpoint filter skips late-delivered historical folders",
        "contracts": ["liveness", "cutoff_integrity"],
        "supported_pipeline_types": ["python_duckdb", "dbt_duckdb"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "S03_retry_batch_append_without_dedup": {
        "description": "Appends retry batches into fact table without grain deduplication",
        "contracts": ["exactly_once"],
        "supported_pipeline_types": ["python_duckdb", "dbt_duckdb"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "S04_delivered_at_beats_version_precedence": {
        "description": "Orders deduplication window by delivered_at DESC instead of version DESC",
        "contracts": ["version_precedence", "exactly_once"],
        "supported_pipeline_types": ["python_duckdb", "dbt_duckdb"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "S05_void_folded_as_zero_row_instead_of_excluded": {
        "description": "Folds void/cancelled events as 0-amount active rows instead of excluding them",
        "contracts": ["version_precedence", "consumer_consistency"],
        "supported_pipeline_types": ["python_duckdb", "dbt_duckdb"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "S06_fanout_join_in_mart_or_bi": {
        "description": "1-to-many join in mart or BI layer multiplies header amounts by line count",
        "contracts": ["exactly_once", "consumer_consistency"],
        "supported_pipeline_types": ["python_duckdb", "dbt_duckdb"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "S07_quality_gate_inner_join_drops_valid_null_fk": {
        "description": "Inner join against optional dimension drops valid records with NULL foreign key",
        "contracts": ["liveness", "consumer_consistency"],
        "supported_pipeline_types": ["python_duckdb", "dbt_duckdb"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "S08_stale_pre_cutover_tax_or_fx_formula": {
        "description": "Applies legacy pre-cutover tax/fee/FX rate formula to post-cutover partitions",
        "contracts": ["version_precedence", "auditability"],
        "supported_pipeline_types": ["python_duckdb", "dbt_duckdb"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "S09_scheduler_dag_dependency_inverted": {
        "description": "Downstream publish/BI refresh executes before upstream fold/dbt finishes on delay",
        "contracts": ["liveness", "consumer_consistency"],
        "supported_pipeline_types": ["python_duckdb", "dbt_duckdb"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "S10_closed_window_unconditional_delete_reinsert": {
        "description": "Publish step deletes and rewrites closed cutoff windows without exception check",
        "contracts": ["cutoff_integrity", "authorization", "auditability"],
        "supported_pipeline_types": ["python_duckdb", "dbt_duckdb"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
}

R_HAZARDS: dict[str, dict[str, Any]] = {
    "H01_delivery_manifest_sidecar": {
        "kind": "arrival_file",
        "description": "_delivery_manifest.csv and _checksums.sha256 in landing folders break naive globs",
        "contracts": ["liveness", "auditability"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "H02_permuted_column_order": {
        "kind": "arrival_file",
        "description": "Future-night retry feeds arrive with permuted column/field order",
        "contracts": ["consumer_consistency", "liveness"],
        "supported_landing_formats": ["csv", "jsonl"],
        "fixed_width_fallback": "H01_delivery_manifest_sidecar",
    },
    "H03_extra_nullable_columns": {
        "kind": "arrival_file",
        "description": "Future-night schema v3.1 adds trailing nullable columns",
        "contracts": ["liveness", "consumer_consistency"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "H04_part_and_temp_files": {
        "kind": "arrival_file",
        "description": "Unfinished .part and ._ temp marker files left by retry daemon in Night 2/3 folders",
        "contracts": ["liveness", "exactly_once"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "H05_utf8_bom_and_crlf_quotes": {
        "kind": "arrival_file",
        "description": "Night 3 export includes UTF-8 BOM on first column header and CRLF quoted fields",
        "contracts": ["liveness", "exactly_once"],
        "supported_landing_formats": ["csv", "jsonl"],
        "fixed_width_fallback": "H04_part_and_temp_files",
    },
    "H06_empty_heartbeat_partition": {
        "kind": "arrival_file",
        "description": "Empty header-only heartbeat file alongside data file breaks non-empty assumptions",
        "contracts": ["liveness"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "H07_backdated_redelivery_in_old_dt": {
        "kind": "arrival_file",
        "description": "Night 3 drops backfill file inside an already-processed historical dt= folder",
        "contracts": ["liveness", "cutoff_integrity"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "H08_intra_file_out_of_order_versions": {
        "kind": "temporal_contract",
        "description": "Single retry file contains version=2 followed later by stale version=1 with newer delivered_at",
        "contracts": ["version_precedence", "exactly_once"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "H09_byte_identical_event_retry": {
        "kind": "temporal_contract",
        "description": "Byte-identical event_id re-delivered on Night 4 testing MAX(delivered_at) join dedup",
        "contracts": ["exactly_once"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "H10_late_void_on_open_vs_closed_window": {
        "kind": "temporal_contract",
        "description": "Void arrives on Night 2 for open window and Night 3 for closed window without ticket",
        "contracts": ["cutoff_integrity", "version_precedence", "authorization"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "H11_Cross_day_timezone_boundary": {
        "kind": "temporal_contract",
        "description": "delivered_at timestamps carry -04:00/+00:00 ISO offsets around 23:30Z clearing cutoff",
        "contracts": ["cutoff_integrity", "auditability"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "H12_idempotent_rerun_double_trigger": {
        "kind": "temporal_contract",
        "description": "Scheduler replays nightly loader twice on Night 2 after simulated lost ack",
        "contracts": ["exactly_once", "auditability"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "H13_superseding_correction_ticket_mid_horizon": {
        "kind": "temporal_contract",
        "description": "Night 2 closes day; Night 3 lands restatement covered by approved vs unapproved ticket",
        "contracts": ["cutoff_integrity", "authorization", "auditability"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
    "H14_null_version_legacy_fallback": {
        "kind": "temporal_contract",
        "description": "Backfilled historical rows carry empty/NULL version requiring default version=1 fallback",
        "contracts": ["version_precedence", "liveness"],
        "supported_landing_formats": ["csv", "jsonl", "fixed_width"],
    },
}

# 15 Curated, High-Signal Coupled Fault Topologies (FT01..FT15)
FAULT_TOPOLOGIES: dict[str, dict[str, Any]] = {
    "FT01": {
        "name": "checkpoint_gap_and_manifest_retry",
        "t0_stressors": [
            "S01_checkpoint_calendar_advance_on_gap",
            "S04_delivered_at_beats_version_precedence",
            "S10_closed_window_unconditional_delete_reinsert",
        ],
        "r_hazards": [
            {"hazard_id": "H01_delivery_manifest_sidecar", "night": 2},
            {"hazard_id": "H08_intra_file_out_of_order_versions", "night": 3},
        ],
        "contracts": [
            "liveness",
            "version_precedence",
            "cutoff_integrity",
            "authorization",
            "auditability",
        ],
    },
    "FT02": {
        "name": "backfill_filter_and_permuted_columns",
        "t0_stressors": [
            "S02_checkpoint_filter_blocks_backfill",
            "S06_fanout_join_in_mart_or_bi",
            "S10_closed_window_unconditional_delete_reinsert",
        ],
        "r_hazards": [
            {"hazard_id": "H02_permuted_column_order", "night": 2},
            {"hazard_id": "H09_byte_identical_event_retry", "night": 4},
        ],
        "contracts": [
            "liveness",
            "exactly_once",
            "cutoff_integrity",
            "consumer_consistency",
            "authorization",
            "auditability",
        ],
    },
    "FT03": {
        "name": "unadjusted_retry_dedup_and_schema_drift",
        "t0_stressors": [
            "S03_retry_batch_append_without_dedup",
        ],
        "r_hazards": [
            {"hazard_id": "H03_extra_nullable_columns", "night": 2},
            {"hazard_id": "H10_late_void_on_open_vs_closed_window", "night": 3},
        ],
        "contracts": [
            "exactly_once",
            "version_precedence",
            "cutoff_integrity",
            "authorization",
        ],
    },
    "FT04": {
        "name": "version_precedence_inversion_and_temp_markers",
        "t0_stressors": [
            "S04_delivered_at_beats_version_precedence",
            "S08_stale_pre_cutover_tax_or_fx_formula",
            "S10_closed_window_unconditional_delete_reinsert",
        ],
        "r_hazards": [
            {"hazard_id": "H04_part_and_temp_files", "night": 2},
            {"hazard_id": "H11_Cross_day_timezone_boundary", "night": 3},
        ],
        "contracts": [
            "version_precedence",
            "exactly_once",
            "cutoff_integrity",
            "auditability",
            "authorization",
        ],
    },
    "FT05": {
        "name": "void_zero_fold_and_bom_double_trigger",
        "t0_stressors": [
            "S05_void_folded_as_zero_row_instead_of_excluded",
            "S03_retry_batch_append_without_dedup",
            "S10_closed_window_unconditional_delete_reinsert",
        ],
        "r_hazards": [
            {"hazard_id": "H05_utf8_bom_and_crlf_quotes", "night": 3},
            {"hazard_id": "H12_idempotent_rerun_double_trigger", "night": 2},
        ],
        "contracts": [
            "version_precedence",
            "exactly_once",
            "consumer_consistency",
            "auditability",
            "cutoff_integrity",
            "authorization",
        ],
    },
    "FT06": {
        "name": "mart_fanout_join_and_mid_horizon_restatement",
        "t0_stressors": [
            "S06_fanout_join_in_mart_or_bi",
        ],
        "r_hazards": [
            {"hazard_id": "H06_empty_heartbeat_partition", "night": 1},
            {"hazard_id": "H13_superseding_correction_ticket_mid_horizon", "night": 3},
        ],
        "contracts": [
            "exactly_once",
            "consumer_consistency",
            "cutoff_integrity",
            "authorization",
        ],
    },
    "FT07": {
        "name": "null_fk_drop_and_legacy_null_version",
        "t0_stressors": [
            "S07_quality_gate_inner_join_drops_valid_null_fk",
            "S04_delivered_at_beats_version_precedence",
            "S10_closed_window_unconditional_delete_reinsert",
        ],
        "r_hazards": [
            {"hazard_id": "H07_backdated_redelivery_in_old_dt", "night": 3},
            {"hazard_id": "H14_null_version_legacy_fallback", "night": 4},
        ],
        "contracts": [
            "liveness",
            "version_precedence",
            "cutoff_integrity",
            "consumer_consistency",
        ],
    },
    "FT08": {
        "name": "cutover_rate_drift_and_out_of_order_retry",
        "t0_stressors": [
            "S08_stale_pre_cutover_tax_or_fx_formula",
            "S03_retry_batch_append_without_dedup",
        ],
        "r_hazards": [
            {"hazard_id": "H01_delivery_manifest_sidecar", "night": 1},
            {"hazard_id": "H09_byte_identical_event_retry", "night": 4},
        ],
        "contracts": [
            "version_precedence",
            "exactly_once",
            "auditability",
            "authorization",
        ],
    },
    "FT09": {
        "name": "dag_inversion_and_closed_window_void",
        "t0_stressors": [
            "S09_scheduler_dag_dependency_inverted",
            "S10_closed_window_unconditional_delete_reinsert",
        ],
        "r_hazards": [
            {"hazard_id": "H04_part_and_temp_files", "night": 2},
            {"hazard_id": "H10_late_void_on_open_vs_closed_window", "night": 3},
        ],
        "contracts": [
            "liveness",
            "cutoff_integrity",
            "consumer_consistency",
            "authorization",
        ],
    },
    "FT10": {
        "name": "closed_window_overwrite_and_tz_boundary",
        "t0_stressors": [
            "S10_closed_window_unconditional_delete_reinsert",
            "S04_delivered_at_beats_version_precedence",
            "S08_stale_pre_cutover_tax_or_fx_formula",
        ],
        "r_hazards": [
            {"hazard_id": "H03_extra_nullable_columns", "night": 2},
            {"hazard_id": "H11_Cross_day_timezone_boundary", "night": 4},
        ],
        "contracts": [
            "cutoff_integrity",
            "version_precedence",
            "authorization",
            "auditability",
        ],
    },
    "FT11": {
        "name": "gap_checkpoint_and_double_trigger_idempotency",
        "t0_stressors": [
            "S01_checkpoint_calendar_advance_on_gap",
            "S05_void_folded_as_zero_row_instead_of_excluded",
            "S10_closed_window_unconditional_delete_reinsert",
        ],
        "r_hazards": [
            {"hazard_id": "H07_backdated_redelivery_in_old_dt", "night": 3},
            {"hazard_id": "H12_idempotent_rerun_double_trigger", "night": 2},
        ],
        "contracts": [
            "liveness",
            "exactly_once",
            "version_precedence",
            "cutoff_integrity",
        ],
    },
    "FT12": {
        "name": "backfill_block_and_mid_horizon_correction",
        "t0_stressors": [
            "S02_checkpoint_filter_blocks_backfill",
            "S08_stale_pre_cutover_tax_or_fx_formula",
        ],
        "r_hazards": [
            {"hazard_id": "H06_empty_heartbeat_partition", "night": 1},
            {"hazard_id": "H13_superseding_correction_ticket_mid_horizon", "night": 3},
        ],
        "contracts": [
            "liveness",
            "cutoff_integrity",
            "authorization",
            "auditability",
        ],
    },
    "FT13": {
        "name": "fanout_and_out_of_order_version_stream",
        "t0_stressors": [
            "S06_fanout_join_in_mart_or_bi",
            "S04_delivered_at_beats_version_precedence",
            "S10_closed_window_unconditional_delete_reinsert",
        ],
        "r_hazards": [
            {"hazard_id": "H02_permuted_column_order", "night": 2},
            {"hazard_id": "H08_intra_file_out_of_order_versions", "night": 4},
        ],
        "contracts": [
            "exactly_once",
            "version_precedence",
            "consumer_consistency",
            "cutoff_integrity",
            "authorization",
            "auditability",
        ],
    },
    "FT14": {
        "name": "null_fk_quality_gate_and_bom_null_version",
        "t0_stressors": [
            "S07_quality_gate_inner_join_drops_valid_null_fk",
            "S09_scheduler_dag_dependency_inverted",
            "S10_closed_window_unconditional_delete_reinsert",
        ],
        "r_hazards": [
            {"hazard_id": "H05_utf8_bom_and_crlf_quotes", "night": 3},
            {"hazard_id": "H14_null_version_legacy_fallback", "night": 2},
        ],
        "contracts": [
            "liveness",
            "version_precedence",
            "consumer_consistency",
        ],
    },
    "FT15": {
        "name": "compositional_cutoff_dedup_and_late_void",
        "t0_stressors": [
            "S01_checkpoint_calendar_advance_on_gap",
            "S03_retry_batch_append_without_dedup",
            "S10_closed_window_unconditional_delete_reinsert",
        ],
        "r_hazards": [
            {"hazard_id": "H08_intra_file_out_of_order_versions", "night": 2},
            {"hazard_id": "H10_late_void_on_open_vs_closed_window", "night": 4},
        ],
        "contracts": [
            "liveness",
            "exactly_once",
            "version_precedence",
            "cutoff_integrity",
            "authorization",
        ],
    },
}

# ---------------------------------------------------------------------------
# Axis 3: 3 Governance & Epistemic Twin Modes
# ---------------------------------------------------------------------------
WITHHOLD_SUBMODES: tuple[str, ...] = (
    "wrong_approver_role",
    "scope_date_mismatch",
    "conditional_prerequisite_unmet",
)

GOVERNANCE_MODES: dict[str, dict[str, Any]] = {
    "act-authorized": {
        "short_code": "auth",
        "twin_role": "control_twin",
        "target_outcome": "resolved",
        "expected_top_band": ["resolved"],
        "submodes": ("owner_exception_valid",),
        "decoy_ticket_count": 4,
    },
    "withhold-blocked": {
        "short_code": "hold",
        "twin_role": "decision_twin",
        "target_outcome": "withheld_with_evidence",
        "expected_top_band": ["withheld_with_evidence"],
        "submodes": WITHHOLD_SUBMODES,
        "decoy_ticket_count": 4,
    },
    "escalate-ambiguous": {
        "short_code": "esc",
        "twin_role": "epistemic_twin",
        "target_outcome": "withheld_with_evidence",
        "expected_top_band": ["withheld_with_evidence", "escalated"],
        "submodes": ("conflicting_authoritative_sources",),
        "decoy_ticket_count": 4,
    },
}

# ---------------------------------------------------------------------------
# Axis 4: 2 Held-Out Schedule Horizons & Lexicon Seeds
# ---------------------------------------------------------------------------
SCHEDULE_SEEDS: dict[str, dict[str, Any]] = {
    "H4_seedA": {
        "short_code": "a",
        "schedule_horizon_hours": 96,
        "nights": 4,
        "lexicon_shift": 0,
        "prompt_variant": "vague",
        "calendar_anchor_date": "2026-06-15",
        "cutoff_date": "2026-06-11",
        "ticket_number_base": 3300,
        "owner_handle_pool": ["rkeane", "mchen", "priya", "stakahashi"],
        "requester_handle_pool": ["nbrennan", "jkovacs", "alopez", "dokafor"],
    },
    "H4_seedB": {
        "short_code": "b",
        "schedule_horizon_hours": 96,
        "nights": 4,
        "lexicon_shift": 1,
        "prompt_variant": "vague",
        "calendar_anchor_date": "2026-09-22",
        "cutoff_date": "2026-09-18",
        "ticket_number_base": 7800,
        "owner_handle_pool": ["vsolis", "kpatel", "hlindqvist", "cmercier"],
        "requester_handle_pool": ["tnguyen", "bwallace", "srostova", "gmensah"],
    },
}


def resolve_topology_for_theme(theme: str, fault_topology_id: str) -> dict[str, Any]:
    """Return a theme-compatible copy of a fault topology.

    Adapts CSV/JSONL-header-specific hazards when scheduled on a fixed_width
    theme (e.g., mainframe_batch) so every blueprint in the matrix satisfies
    strict surface compatibility.
    """
    theme_meta = THEMES[theme]
    base_ft = FAULT_TOPOLOGIES[fault_topology_id]
    landing_fmt = theme_meta["landing_format"]

    t0_stressors = list(base_ft["t0_stressors"])
    r_hazards: list[dict[str, Any]] = []
    used_hazard_ids: set[str] = set()

    for h_entry in base_ft["r_hazards"]:
        h_id = h_entry["hazard_id"]
        h_spec = R_HAZARDS[h_id]
        if landing_fmt not in h_spec["supported_landing_formats"]:
            fallback_id = h_spec.get("fixed_width_fallback", "H01_delivery_manifest_sidecar")
            if fallback_id in used_hazard_ids:
                fallback_id = "H07_backdated_redelivery_in_old_dt"
            h_id = fallback_id
        used_hazard_ids.add(h_id)
        r_hazards.append({"hazard_id": h_id, "night": int(h_entry["night"])})

    contracts = list(base_ft["contracts"])
    return {
        "fault_topology_id": fault_topology_id,
        "name": base_ft["name"],
        "t0_stressors": t0_stressors,
        "r_hazards": r_hazards,
        "contracts": contracts,
    }


def is_blueprint_compatible(entry: dict[str, Any]) -> tuple[bool, list[str]]:
    """Verify that a blueprint's stressors and hazards are compatible with its theme."""
    errors: list[str] = []
    theme = entry.get("theme")
    if theme not in THEMES:
        return False, [f"Unknown theme: {theme!r}"]

    theme_meta = THEMES[theme]
    pipeline_type = theme_meta["pipeline_type"]
    landing_format = theme_meta["landing_format"]

    t0_stressors = entry.get("t0_stressors", [])
    if not (1 <= len(t0_stressors) <= 3):
        errors.append(f"Expected 1 to 3 t0_stressors, got {len(t0_stressors)}")

    for s_id in t0_stressors:
        if s_id not in T0_STRESSORS:
            errors.append(f"Unknown t0_stressor: {s_id!r}")
            continue
        s_spec = T0_STRESSORS[s_id]
        if pipeline_type not in s_spec["supported_pipeline_types"]:
            errors.append(
                f"Stressor {s_id} incompatible with pipeline_type {pipeline_type} on {theme}"
            )
        if landing_format not in s_spec["supported_landing_formats"]:
            errors.append(
                f"Stressor {s_id} incompatible with landing_format {landing_format} on {theme}"
            )

    r_hazards = entry.get("r_hazards", [])
    if len(r_hazards) != 2:
        errors.append(f"Expected exactly 2 r_hazards, got {len(r_hazards)}")

    seen_hazards: set[str] = set()
    for h_item in r_hazards:
        h_id = h_item.get("hazard_id")
        night = h_item.get("night")
        if h_id not in R_HAZARDS:
            errors.append(f"Unknown r_hazard: {h_id!r}")
            continue
        if h_id in seen_hazards:
            errors.append(f"Duplicate hazard {h_id} in same blueprint")
        seen_hazards.add(h_id)
        if night not in (1, 2, 3, 4):
            errors.append(f"Hazard {h_id} night must be in 1..4, got {night!r}")
        h_spec = R_HAZARDS[h_id]
        if landing_format not in h_spec["supported_landing_formats"]:
            errors.append(
                f"Hazard {h_id} incompatible with landing_format {landing_format} on {theme}"
            )

    for contract in entry.get("contracts", []):
        if contract not in VALID_CONTRACTS:
            errors.append(f"Unknown contract: {contract!r}")

    return len(errors) == 0, errors


def generate_catalog(out_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Generate the deterministic 1,080-blueprint catalog across the 4 orthogonal axes.

    If `out_path` is provided, writes the catalog in JSONL format to `out_path`.
    """
    catalog: list[dict[str, Any]] = []
    blueprint_index = 0
    twin_group_index = 0

    theme_names = list(THEMES.keys())
    topology_ids = list(FAULT_TOPOLOGIES.keys())
    schedule_keys = list(SCHEDULE_SEEDS.keys())
    governance_keys = list(GOVERNANCE_MODES.keys())

    for theme_idx, theme in enumerate(theme_names):
        theme_meta = THEMES[theme]
        theme_slug = theme.replace("_", "-")

        for ft_idx, ft_id in enumerate(topology_ids):
            resolved_ft = resolve_topology_for_theme(theme, ft_id)
            ft_slug = ft_id.lower()

            for sched_idx, sched_key in enumerate(schedule_keys):
                sched_meta = SCHEDULE_SEEDS[sched_key]
                seed_short = sched_meta["short_code"]
                twin_group_index += 1
                twin_group_id = (
                    f"TG{twin_group_index:04d}-{theme_slug}-{ft_slug}-{seed_short}"
                )

                # Deterministic submode rotation across twin groups for withhold-blocked
                withhold_submode = WITHHOLD_SUBMODES[
                    (theme_idx + ft_idx + sched_idx) % len(WITHHOLD_SUBMODES)
                ]

                # Deterministic integer seed shared by the twin group's underlying world/schedule
                base_seed = (
                    100_000
                    + theme_idx * 5_000
                    + ft_idx * 200
                    + sched_idx * 97
                )

                owner_handle = sched_meta["owner_handle_pool"][
                    (theme_idx + ft_idx) % len(sched_meta["owner_handle_pool"])
                ]
                requester_handle = sched_meta["requester_handle_pool"][
                    (theme_idx + ft_idx) % len(sched_meta["requester_handle_pool"])
                ]
                primary_ticket_num = sched_meta["ticket_number_base"] + ft_idx * 7 + 5
                primary_ticket_id = f"{theme_meta['project_prefix']}-{primary_ticket_num}"

                for gov_mode in governance_keys:
                    blueprint_index += 1
                    gov_meta = GOVERNANCE_MODES[gov_mode]
                    gov_short = gov_meta["short_code"]

                    if gov_mode == "withhold-blocked":
                        gov_submode = withhold_submode
                    else:
                        gov_submode = gov_meta["submodes"][0]

                    task_id = (
                        f"FT{blueprint_index:04d}-{theme_slug}-{ft_slug}-{gov_short}-{seed_short}"
                    )

                    entry: dict[str, Any] = {
                        "task_id": task_id,
                        "blueprint_index": blueprint_index,
                        "twin_group_id": twin_group_id,
                        "theme": theme,
                        "theme_meta": dict(theme_meta),
                        "fault_topology_id": ft_id,
                        "topology_id": ft_id,
                        "fault_topology_name": resolved_ft["name"],
                        "topology_label": resolved_ft["name"],
                        "t0_stressors": list(resolved_ft["t0_stressors"]),
                        "t0_stressor_codes": [
                            s.split("_", 1)[0] for s in resolved_ft["t0_stressors"]
                        ],
                        "r_hazards": [dict(h) for h in resolved_ft["r_hazards"]],
                        "hazards": [
                            h["hazard_id"].split("_", 1)[0]
                            for h in resolved_ft["r_hazards"]
                        ],
                        "contracts": list(resolved_ft["contracts"]),
                        "governance_mode": gov_mode,
                        "governance_submode": gov_submode,
                        "target_outcome": gov_meta["target_outcome"],
                        "expected_top_band": list(gov_meta["expected_top_band"]),
                        "schedule_seed_id": sched_key,
                        "schedule_horizon_hours": sched_meta["schedule_horizon_hours"],
                        "nights": sched_meta["nights"],
                        "seed": base_seed,
                        "lexicon_seed": base_seed + (11 if sched_meta["lexicon_shift"] == 0 else 29),
                        "lexicon_shift": sched_meta["lexicon_shift"],
                        "schedule": {
                            "id": sched_key,
                            "nights": sched_meta["nights"],
                            "hazard_nights": [h["night"] for h in resolved_ft["r_hazards"]],
                            "lexicon_salt": 11 if sched_meta["lexicon_shift"] == 0 else 29,
                        },
                        "composition_surface": "factory_sidecar",
                        "theme_surface": "native_clean_world",
                        "verification_status": "candidate",
                        "schema_version": 1,
                        "lexicon_config": {
                            "calendar_anchor_date": sched_meta["calendar_anchor_date"],
                            "cutoff_date": sched_meta["cutoff_date"],
                            "project_prefix": theme_meta["project_prefix"],
                            "primary_ticket_id": primary_ticket_id,
                            "owner_handle": owner_handle,
                            "requester_handle": requester_handle,
                        },
                        "prompt_variant": sched_meta["prompt_variant"],
                        "difficulty_knobs": {
                            "decoy_ticket_count": gov_meta["decoy_ticket_count"],
                            "t0_stressor_count": len(resolved_ft["t0_stressors"]),
                            "r_hazard_count": len(resolved_ft["r_hazards"]),
                            "governance_hops": 2 if gov_mode == "act-authorized" else 3,
                            "requires_multi_night_retention": True,
                            "lexicon_shifted": sched_meta["lexicon_shift"] == 1,
                        },
                    }
                    catalog.append(entry)

    if out_path is not None:
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as fh:
            for item in catalog:
                fh.write(json.dumps(item, sort_keys=False) + "\n")

    return catalog


compile_catalog = generate_catalog


def select_stratified_batch(
    catalog: list[dict[str, Any]],
    per_theme: int = 3,
) -> list[dict[str, Any]]:
    """Select a balanced batch covering all 12 themes and rotating across topologies/modes.

    When `per_theme == 3` (default), returns 36 blueprints (12 themes x 3 governance modes)
    where each theme gets one complete paired twin group (`act-authorized`,
    `withhold-blocked`, `escalate-ambiguous`) and fault topologies + schedule seeds rotate
    across themes so all 12 themes, all 3 governance modes, all 3 withhold sub-modes,
    and both schedule seeds are represented.
    """
    if per_theme <= 0:
        raise ValueError(f"per_theme must be positive, got {per_theme}")

    by_theme_and_group: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for entry in catalog:
        by_theme_and_group[entry["theme"]][entry["twin_group_id"]].append(entry)

    selected: list[dict[str, Any]] = []
    gov_order = list(GOVERNANCE_MODES.keys())

    for theme_idx, theme in enumerate(THEMES.keys()):
        groups_map = by_theme_and_group[theme]
        group_ids = list(groups_map.keys())
        if not group_ids:
            continue

        picked_for_theme: list[dict[str, Any]] = []
        group_offset = (theme_idx * 2) % len(group_ids)

        while len(picked_for_theme) < per_theme:
            g_id = group_ids[group_offset % len(group_ids)]
            group_entries = {e["governance_mode"]: e for e in groups_map[g_id]}
            for g_mode in gov_order:
                if len(picked_for_theme) >= per_theme:
                    break
                if g_mode in group_entries:
                    picked_for_theme.append(group_entries[g_mode])
            group_offset += 1

        selected.extend(picked_for_theme)

    return selected


def validate_catalog(catalog: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate the 1,080-blueprint catalog and return summary statistics."""
    errors: list[str] = []

    expected_total = (
        len(THEMES) * len(FAULT_TOPOLOGIES) * len(GOVERNANCE_MODES) * len(SCHEDULE_SEEDS)
    )
    if len(catalog) != expected_total:
        errors.append(f"Expected {expected_total} blueprints, got {len(catalog)}")

    task_ids = [e.get("task_id") for e in catalog]
    if len(set(task_ids)) != len(catalog):
        errors.append(
            f"Duplicate task_ids detected: {len(set(task_ids))} unique out of {len(catalog)}"
        )

    indices = [e.get("blueprint_index") for e in catalog]
    if indices != list(range(1, len(catalog) + 1)):
        errors.append("blueprint_index is not strictly 1..N sequential")

    # Twin group validation: 360 twin groups, each of size 3 with identical (theme, FT, schedule_seed)
    twin_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in catalog:
        twin_groups[entry["twin_group_id"]].append(entry)

    expected_groups = len(THEMES) * len(FAULT_TOPOLOGIES) * len(SCHEDULE_SEEDS)
    if len(twin_groups) != expected_groups:
        errors.append(
            f"Expected {expected_groups} twin groups, got {len(twin_groups)}"
        )

    for tg_id, members in twin_groups.items():
        if len(members) != 3:
            errors.append(f"Twin group {tg_id} has size {len(members)} != 3")
            continue
        modes = {m["governance_mode"] for m in members}
        if modes != set(GOVERNANCE_MODES.keys()):
            errors.append(f"Twin group {tg_id} missing governance modes: {modes}")
        themes = {m["theme"] for m in members}
        fts = {m["fault_topology_id"] for m in members}
        seeds = {m["seed"] for m in members}
        if len(themes) != 1 or len(fts) != 1 or len(seeds) != 1:
            errors.append(
                f"Twin group {tg_id} members disagree on (theme, fault_topology_id, seed)"
            )

    # Compatibility and coverage checks
    theme_counts: Counter[str] = Counter()
    topology_counts: Counter[str] = Counter()
    gov_counts: Counter[str] = Counter()
    submode_counts: Counter[str] = Counter()
    stressor_counts: Counter[str] = Counter()
    hazard_counts: Counter[str] = Counter()
    lexicon_counts: Counter[int] = Counter()

    for entry in catalog:
        ok, entry_errors = is_blueprint_compatible(entry)
        if not ok:
            errors.extend(
                [f"{entry.get('task_id')}: {err}" for err in entry_errors]
            )
        theme_counts[entry["theme"]] += 1
        topology_counts[entry["fault_topology_id"]] += 1
        gov_counts[entry["governance_mode"]] += 1
        submode_counts[entry["governance_submode"]] += 1
        lexicon_counts[entry["lexicon_shift"]] += 1
        for s_id in entry["t0_stressors"]:
            stressor_counts[s_id] += 1
        for h_item in entry["r_hazards"]:
            hazard_counts[h_item["hazard_id"]] += 1

    missing_themes = set(THEMES.keys()) - set(theme_counts.keys())
    if missing_themes:
        errors.append(f"Missing themes in catalog: {sorted(missing_themes)}")

    missing_topologies = set(FAULT_TOPOLOGIES.keys()) - set(topology_counts.keys())
    if missing_topologies:
        errors.append(f"Missing topologies in catalog: {sorted(missing_topologies)}")

    missing_stressors = set(T0_STRESSORS.keys()) - set(stressor_counts.keys())
    if missing_stressors:
        errors.append(f"Missing t0 stressors in catalog: {sorted(missing_stressors)}")

    missing_hazards = set(R_HAZARDS.keys()) - set(hazard_counts.keys())
    if missing_hazards:
        errors.append(f"Missing r_hazards in catalog: {sorted(missing_hazards)}")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "total_blueprints": len(catalog),
        "unique_task_ids": len(set(task_ids)),
        "twin_group_count": len(twin_groups),
        "theme_counts": dict(theme_counts),
        "topology_counts": dict(topology_counts),
        "governance_mode_counts": dict(gov_counts),
        "governance_submode_counts": dict(submode_counts),
        "stressor_counts": dict(stressor_counts),
        "hazard_counts": dict(hazard_counts),
        "lexicon_shift_counts": dict(lexicon_counts),
    }


def main() -> None:
    catalog = generate_catalog(out_path=DEFAULT_CATALOG_PATH)
    report = validate_catalog(catalog)
    if not report["valid"]:
        raise SystemExit(f"Catalog validation failed: {report['errors']}")
    print(
        json.dumps(
            {
                "catalog_path": str(DEFAULT_CATALOG_PATH),
                "total_blueprints": report["total_blueprints"],
                "twin_group_count": report["twin_group_count"],
                "themes": len(report["theme_counts"]),
                "topologies": len(report["topology_counts"]),
                "governance_modes": report["governance_mode_counts"],
                "governance_submodes": report["governance_submode_counts"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
