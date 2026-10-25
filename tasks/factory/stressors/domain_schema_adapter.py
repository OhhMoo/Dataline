"""Bind a theme's real contract to the factory feed pipeline.

Table, grain, and script names come from ``theme_contract.yaml`` when that
file exists, and from pipeline files that are actually on disk. A null
contract surface stays null. This module does not create tables.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


THEMES: tuple[str, ...] = (
    "commerce_analytics",
    "revenue_daily",
    "sftp_vendor",
    "mainframe_batch",
    "excel_close",
    "settlement_clearing",
    "saas_subscription_mrr",
    "ad_attribution_events",
    "supply_chain_inventory",
    "healthcare_claims_edi",
    "fintech_kyc_risk_limits",
    "iot_telemetry_billing",
)

# Workspace-relative candidates. A path is recorded only when the file exists.
_SCRIPT_CANDIDATES: dict[str, dict[str, str]] = {
    "settlement_clearing": {
        "ingest": "pipelines/load_payments.py",
        "fold": "dbt_project/models/warehouse/fct_payments.sql",
        "publish": "pipelines/publish_settlement.py",
        "close": "pipelines/clearing_window_close.py",
    },
    "commerce_analytics": {
        "ingest": "loader/load_extracts.py",
        "fold": "dbt_project/models/marts/fct_orders.sql",
        "publish": "scripts/refresh_dashboards.py",
    },
    "revenue_daily": {
        "ingest": "loader/load_daily_revenue.py",
        "fold": "dbt_project/models/warehouse/fct_revenue.sql",
    },
    "sftp_vendor": {
        "ingest": "loader/vendor_load.py",
        "fold": "dbt_project/models/warehouse/fct_orders_daily.sql",
    },
    "mainframe_batch": {
        "ingest": "loader/load_extract.py",
        "fold": "dbt_project/models/warehouse/fct_claims_daily.sql",
    },
    "excel_close": {
        "ingest": "loader/import_close.py",
        "fold": "dbt_project/models/warehouse/fct_gl_monthly.sql",
    },
    "saas_subscription_mrr": {
        "ingest": "loader/ingest_webhooks.py",
        "fold": "dbt_project/models/warehouse/fct_mrr_daily.sql",
        "close": "dbt_project/models/warehouse/revrec_close_window.sql",
    },
    "ad_attribution_events": {
        "ingest": "loader/load_events.py",
        "fold": "dbt_project/models/warehouse/fct_attributed_conversions.sql",
        "publish": "dbt_project/models/warehouse/partner_payout_handoff.sql",
        "close": "dbt_project/models/warehouse/billing_lock.sql",
    },
    "supply_chain_inventory": {
        "ingest": "loader/load_inventory.py",
        "fold": "dbt_project/models/warehouse/fct_inventory_position.sql",
        "publish": "scripts/export_threepl.py",
        "close": "dbt_project/models/warehouse/customs_cutoff.sql",
    },
    "healthcare_claims_edi": {
        "ingest": "loader/load_edi.py",
        "fold": "dbt_project/models/warehouse/fct_adjudicated_claims.sql",
        "publish": "dbt_project/models/warehouse/actuarial_reserve_handoff.sql",
        "close": "dbt_project/models/warehouse/cms_submission_window.sql",
    },
    "fintech_kyc_risk_limits": {
        "ingest": "loader/load_risk_feeds.py",
        "fold": "dbt_project/models/warehouse/fct_merchant_exposure.sql",
        "publish": "dbt_project/models/warehouse/treasury_reserve_handoff.sql",
        "close": "dbt_project/models/warehouse/risk_committee_signoff.sql",
    },
    "iot_telemetry_billing": {
        "ingest": "loader/load_usage.py",
        "fold": "dbt_project/models/warehouse/fct_billable_usage.sql",
        "publish": "dbt_project/models/warehouse/invoice_run_handoff.sql",
        "close": "dbt_project/models/warehouse/billing_period_lock.sql",
    },
}

# Files the composer writes. The compiler looks for these exact names.
EMITTED_MODULES: tuple[str, ...] = (
    "ingest_feed.py",
    "fold_ledger.py",
    "publish_handoff.py",
)

# Feed columns the minimal DuckDB world actually stores. Theme grain is
# reported beside this; it is not renamed onto a table the contract does not
# describe.
FEED_GRAIN = {
    "raw": ["event_id"],
    "fact": ["business_id"],
    "handoff": ["business_day", "entity_id"],
    "checkpoint": ["through_day"],
}

# Used only when a contract surface is null, or when a built theme already
# owns the native table with a different column set. These names are not
# listed in the declared grader tables.
FALLBACK_TABLES = {
    "raw": "raw.factory_events",
    "fact": "warehouse.fct_factory_events",
    "handoff": "warehouse.factory_handoff",
    "close": "warehouse.factory_close",
    "corrections": "warehouse.factory_corrections",
    "checkpoint": "warehouse.factory_checkpoint",
}

DECLARED_FORBIDDEN = frozenset({
    "raw.factory_events",
    "warehouse.fct_factory_events",
    "warehouse.factory_handoff",
})


def _themes_root() -> Path:
    return Path(__file__).resolve().parents[3] / "world" / "world" / "themes"


def _blank(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class DomainBinding:
    theme: str
    landing_dir: str
    raw_table: str
    fact_table: str
    handoff_table: str | None
    close_table: str | None
    corrections_table: str | None
    grain_keys: tuple[str, ...]
    amount_column: str | None
    version_column: str | None
    cutoff_time_utc: str | None
    scheduled_jobs: tuple[str, ...]
    native_scripts: dict[str, str]
    contract_path: str

    @property
    def modules(self) -> tuple[str, ...]:
        return EMITTED_MODULES

    def preferred_tables(self) -> dict[str, str]:
        """Physical names to create when the warehouse does not already have them."""
        return {
            "raw": self.raw_table,
            "fact": self.fact_table,
            "handoff": self.handoff_table or FALLBACK_TABLES["handoff"],
            "close": self.close_table or FALLBACK_TABLES["close"],
            "corrections": self.corrections_table or FALLBACK_TABLES["corrections"],
            "checkpoint": FALLBACK_TABLES["checkpoint"],
        }

    def declared_tables(self) -> list[str]:
        """Native contract tables. Omits null surfaces and factory fallbacks."""
        tables = [self.raw_table, self.fact_table]
        for name in (self.handoff_table, self.close_table, self.corrections_table):
            if name and name not in DECLARED_FORBIDDEN and name not in tables:
                tables.append(name)
        return tables

    def as_dict(self) -> dict:
        return {
            "theme": self.theme,
            "landing_dir": self.landing_dir,
            "raw_table": self.raw_table,
            "fact_table": self.fact_table,
            "handoff_table": self.handoff_table,
            "close_table": self.close_table,
            "corrections_table": self.corrections_table,
            "grain_keys": list(self.grain_keys),
            "amount_column": self.amount_column,
            "version_column": self.version_column,
            "cutoff_time_utc": self.cutoff_time_utc,
            "scheduled_jobs": list(self.scheduled_jobs),
            "native_scripts": dict(self.native_scripts),
            "modules": list(self.modules),
            "feed_grain": FEED_GRAIN,
            "contract_path": self.contract_path,
        }


def load_theme_binding(theme: str, root: Path | None = None) -> DomainBinding:
    """Read one theme contract. Raises if the theme or a required table is missing."""
    name = str(theme).strip()
    if name not in THEMES:
        raise ValueError(f"unknown theme {name!r}; expected one of {list(THEMES)}")
    theme_dir = (root or _themes_root()) / name
    contract_path = theme_dir / "theme_contract.yaml"
    if not contract_path.is_file():
        raise ValueError(f"{name} has no theme_contract.yaml at {contract_path}")
    document = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise ValueError(f"{contract_path} is not a mapping")
    if _blank(document.get("theme")) not in (None, name):
        raise ValueError(f"{contract_path} declares theme {document.get('theme')!r}")
    raw_table = _blank(document.get("raw_table"))
    fact_table = _blank(document.get("fact_table"))
    landing = _blank(document.get("landing_dir"))
    if not raw_table or not fact_table or not landing:
        raise ValueError(f"{name} contract is missing raw_table, fact_table, or landing_dir")
    grain = document.get("grain_keys") or []
    if isinstance(grain, str):
        grain = [grain]
    jobs = document.get("scheduled_jobs") or []
    if isinstance(jobs, str):
        jobs = [jobs]
    scripts: dict[str, str] = {}
    workspace = theme_dir / "workspace"
    for role, relative in _SCRIPT_CANDIDATES.get(name, {}).items():
        if (workspace / relative).is_file():
            scripts[role] = relative
    return DomainBinding(
        theme=name,
        landing_dir=landing,
        raw_table=raw_table,
        fact_table=fact_table,
        handoff_table=_blank(document.get("handoff_table")),
        close_table=_blank(document.get("close_window_table")),
        corrections_table=_blank(document.get("corrections_table")),
        grain_keys=tuple(str(item) for item in grain),
        amount_column=_blank(document.get("amount_column")),
        version_column=_blank(document.get("version_column")),
        cutoff_time_utc=_blank(document.get("cutoff_time_utc")),
        scheduled_jobs=tuple(str(item) for item in jobs),
        native_scripts=scripts,
        contract_path=str(contract_path),
    )


def load_all_bindings(root: Path | None = None) -> dict[str, DomainBinding]:
    return {name: load_theme_binding(name, root) for name in THEMES}
