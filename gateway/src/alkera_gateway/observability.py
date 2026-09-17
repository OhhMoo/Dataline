"""Metrics, alarms and canaried deploys (see OBSERVABILITY_CONTRACT.md).

The single rule this module exists to keep: **every number is read from the
world at the moment it is asked for.** There is no stored "current value" for a
metric and no stored "state" for an alarm. A metric is a declared reading -
a warehouse query or a scheduler-history derivation - and evaluating it runs
that reading against live state. An alarm is a declared comparison over such a
reading, and its state is computed, never cached. A surface that reports a
value it did not just measure is a lie the grader would believe, so this module
has no code path that can produce one.

That rule is about **now**. History is the other half, and it works the other
way round: `metrics_series` and `alarms_history` read the world's recorded
sample store (`/var/obs/samples.jsonl`, OBS_STORE_CONTRACT.md) instead of
recomputing, because once `advance` has run the state that produced yesterday's
reading is gone and the recorded sample is the only evidence it ever held that
value. **Now is computed; then is remembered.** A derivation run today describes
the world as it stands today, so it is not offered as history - if it were, the
store would be decoration and reading the dashboard would be strictly dominated
by reading the data.

The record has a declared retention window. That is deliberate: a record with no
limit is an oracle, and "the sample I needed aged out" is an honest situation.
What the window excludes is counted and reported, never silently dropped.

The gateway never writes `samples.jsonl` - the world does, during build and
advance. What the gateway persists, in `obs_state_dir` beside the audit log and
never inside the fingerprinted world tree, is the record of what *this episode*
observed: samples its own calls took, alarm transitions it saw, deploy records.

Definitions live in the world at `observability.yml` so a task author can plant
them. Author-side fields (`known_imprecision`) are stripped from every tool
response the same way the ingest connector's `sync` template is - the agent
must not be told which alarm was designed to mislead it.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .records import Observation, ToolError, canonical, digest

# Kinds whose *scalar* value is a function of the virtual clock, and can
# therefore honestly be re-evaluated "as of" an earlier time.
CLOCK_KINDS = {"freshness", "job_duration", "job_error_rate"}
# Kinds that carry a real historical series without a declaration, because the
# scheduler's run history already is one.
RUN_SERIES_KINDS = {"job_duration", "job_error_rate"}
SAMPLE_CAP = 200
IDENTIFIER = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")
# The recorded sample store (OBS_STORE_CONTRACT.md). The world writes it; this
# module only reads. Retention is a *read-side* window: the file is append-only
# and is never rewritten, so ageing out is what the reader refuses to serve, and
# the count it refused is reported rather than hidden.
DEFAULT_RETENTION_DAYS = 14
MALFORMED_CAP = 20
# The alternate definitions filename the store contract names. The gateway's own
# default is observability.yml; a world that seeds definitions.yaml is read too,
# so the two documents cannot drift into a world that answers no_observability.
ALTERNATE_DEFINITIONS = "definitions.yaml"
# The operator's own edits to the alarm surface, beside the definitions and
# inside the world tree (see the silences block in `ObservabilityStore`).
SILENCES_FILE = "silences.json"


def parse_vts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sql_timestamp(moment: datetime) -> str:
    """A naive UTC literal. The world has one timezone and every stored value is
    naive, so a tz-qualified literal would compare against a different thing."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def qualified(name: str, what: str) -> str:
    """Reject anything that is not a plain (optionally dotted) identifier.

    Metric definitions are world-authored, so this is not an injection defence;
    it is a defence against a malformed definition producing SQL that happens to
    parse into a different question than the one the author asked."""
    parts = name.split(".")
    if not parts or any(not part or set(part) - IDENTIFIER for part in parts):
        raise ToolError("invalid_observability", f"{what} is not a plain identifier: {name!r}")
    return name


def number(cell):
    """A metric value must be numeric or absent; never a coerced guess."""
    if isinstance(cell, bool) or cell is None:
        return None
    if isinstance(cell, (int, float)):
        return float(cell)
    if isinstance(cell, dict) and cell.get("type") == "decimal":
        return float(cell["value"])
    if isinstance(cell, str):
        try:
            return float(cell)
        except ValueError:
            return None
    return None


def moment(cell):
    if not isinstance(cell, str):
        return None
    try:
        return parse_vts(cell)
    except ValueError:
        return None


class MetricSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str | None = None
    column: str | None = None
    timestamp_column: str | None = None
    job: str | None = None
    window_runs: int | None = Field(default=None, ge=1, le=1000)
    sql: str | None = None


class MetricSeries(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bucket_column: str | None = None
    sql: str | None = None


class MetricDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: Literal[
        "freshness", "row_count", "null_rate", "job_duration", "job_error_rate", "sql_scalar"
    ]
    unit: str
    title: str | None = None
    description: str | None = None
    source: MetricSource
    series: MetricSeries | None = None

    @model_validator(mode="before")
    @classmethod
    def lift_source_aliases(cls, data):
        """Accept the world writer's spelling as well as this module's.

        `world/world/observe.py` reads a job metric as either `metric.job` or
        `metric.source.job`, and reads `window_runs` only at the top level. One
        definitions file has to satisfy both sides of the seam, so a top-level
        source key is lifted into `source` here rather than rejected. Anything
        genuinely unrecognised still fails the whole call - a broken definition
        is a broken surface, and that stays loud."""
        if not isinstance(data, dict):
            return data
        lifted = {
            key: data[key]
            for key in ("table", "column", "timestamp_column", "job", "window_runs", "sql")
            if key in data
        }
        if not lifted:
            return data
        data = dict(data)
        source = dict(data.get("source") or {})
        for key, value in lifted.items():
            data.pop(key)
            source.setdefault(key, value)
        data["source"] = source
        return data

    @model_validator(mode="after")
    def check_source(self):
        needed = {
            "freshness": ("table", "timestamp_column"),
            "row_count": ("table",),
            "null_rate": ("table", "column"),
            "job_duration": ("job",),
            "job_error_rate": ("job",),
            "sql_scalar": ("sql",),
        }[self.kind]
        for field in needed:
            if getattr(self.source, field) is None:
                raise ValueError(f"metric {self.id}: kind {self.kind} needs source.{field}")
        if self.kind in RUN_SERIES_KINDS and self.series is not None:
            raise ValueError(f"metric {self.id}: {self.kind} takes its series from run history")
        if self.series is not None and (self.series.bucket_column is None) == (
            self.series.sql is None
        ):
            raise ValueError(f"metric {self.id}: series needs exactly one of bucket_column, sql")
        if self.series is not None and self.series.bucket_column is not None:
            if self.kind not in {"row_count", "null_rate"}:
                raise ValueError(f"metric {self.id}: bucket_column suits row_count and null_rate")
        return self

    @property
    def has_series(self) -> bool:
        return self.kind in RUN_SERIES_KINDS or self.series is not None


class Imprecision(BaseModel):
    """Author-side. Stripped from every tool response; see OBSERVABILITY_CONTRACT.md."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["false_positive", "blind_spot", "both"]
    note: str


class Transition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at: str
    state: Literal["ok", "firing", "pending", "insufficient_data"]
    value: float | None = None
    note: str | None = None


class AlarmDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    metric: str
    comparison: Literal["gt", "gte", "lt", "lte"]
    threshold: float
    severity: Literal["info", "warning", "critical"] = "warning"
    title: str | None = None
    description: str | None = None
    runbook: str | None = None
    evaluate_on: Literal["value", "series"] = "value"
    datapoints_to_alarm: int = Field(default=1, ge=1, le=100)
    evaluation_lag_s: int = Field(default=0, ge=0)
    known_imprecision: Imprecision | None = None
    history: list[Transition] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_evaluation(self):
        if self.datapoints_to_alarm > 1 and self.evaluate_on != "series":
            raise ValueError(
                f"alarm {self.id}: datapoints_to_alarm > 1 needs evaluate_on: series, because "
                "consecutive datapoints only exist on a series"
            )
        return self


class Guard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: str
    max_increase_pct: float | None = Field(default=None, ge=0)
    max_decrease_pct: float | None = Field(default=None, ge=0)
    max_increase_abs: float | None = Field(default=None, ge=0)
    max_decrease_abs: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_bound(self):
        if not any(
            getattr(self, name) is not None
            for name in (
                "max_increase_pct",
                "max_decrease_pct",
                "max_increase_abs",
                "max_decrease_abs",
            )
        ):
            raise ValueError(f"guard on {self.metric}: name at least one bound")
        return self


class Canary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metrics: list[str] = Field(default_factory=list)
    alarms: list[str] = Field(default_factory=list)
    guards: list[Guard] = Field(default_factory=list)


class DeployDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: Literal["dbt", "sql"]
    title: str | None = None
    description: str | None = None
    selector: str | None = None
    statements: list[str] = Field(default_factory=list)
    canary: Canary = Field(default_factory=Canary)

    @model_validator(mode="after")
    def check_kind(self):
        if self.kind == "sql" and not self.statements:
            raise ValueError(f"deploy {self.id}: kind sql needs statements")
        if self.kind == "dbt" and self.statements:
            raise ValueError(f"deploy {self.id}: kind dbt takes a selector, not statements")
        return self


class Retention(BaseModel):
    """How far back the recorded store is allowed to answer.

    A record with no limit is an oracle, and an oracle is not a thing any real
    observability system is. The window is declared by the world next to the
    metrics it applies to, so "the sample I needed aged out" is a situation the
    task author chooses rather than an accident of how long the file happens to
    be."""

    model_config = ConfigDict(extra="forbid")
    days: int = Field(default=DEFAULT_RETENTION_DAYS, ge=1, le=3650)


class SampleRow(BaseModel):
    """One line of /var/obs/samples.jsonl, per OBS_STORE_CONTRACT.md.

    `value` is *required* and may be `null`. That is the whole point of the
    field: a missing key is a malformed row, not a zero, and a `null` is
    "insufficient data at that moment", which is a real thing to have recorded.
    Unknown keys are ignored rather than refused, so the writer can add fields
    without this reader rejecting the world's history."""

    model_config = ConfigDict(extra="ignore")
    vts: str
    metric: str
    value: float | None
    source: Literal["advance", "trigger", "deploy", "build"] | None = None
    run_id: str | None = None
    detail: dict = Field(default_factory=dict)


class ObservabilitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(default=1, ge=1, le=1)
    retention: Retention = Field(default_factory=Retention)
    metrics: list[MetricDef] = Field(default_factory=list)
    alarms: list[AlarmDef] = Field(default_factory=list)
    deploys: list[DeployDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_references(self):
        for group in (self.metrics, self.alarms, self.deploys):
            names = [item.id for item in group]
            if len(set(names)) != len(names):
                raise ValueError("duplicate id in observability definitions")
        metrics = {metric.id: metric for metric in self.metrics}
        alarms = {alarm.id for alarm in self.alarms}
        for alarm in self.alarms:
            metric = metrics.get(alarm.metric)
            if metric is None:
                raise ValueError(f"alarm {alarm.id}: unknown metric {alarm.metric}")
            if alarm.evaluate_on == "series" and not metric.has_series:
                raise ValueError(f"alarm {alarm.id}: metric {metric.id} has no series")
            # Lag is only honest where an earlier reading is genuinely derivable:
            # from the clock, or by dropping series points later than `as_of`.
            if alarm.evaluation_lag_s and not (
                alarm.evaluate_on == "series" or metric.kind in CLOCK_KINDS
            ):
                raise ValueError(
                    f"alarm {alarm.id}: evaluation_lag_s cannot be applied to a {metric.kind} "
                    "value, because no earlier value can be derived from current state"
                )
        for deploy in self.deploys:
            for name in deploy.canary.metrics + [g.metric for g in deploy.canary.guards]:
                if name not in metrics:
                    raise ValueError(f"deploy {deploy.id}: unknown canary metric {name}")
            for name in deploy.canary.alarms:
                if name not in alarms:
                    raise ValueError(f"deploy {deploy.id}: unknown canary alarm {name}")
        return self


BREACH = {
    "gt": lambda value, threshold: value > threshold,
    "gte": lambda value, threshold: value >= threshold,
    "lt": lambda value, threshold: value < threshold,
    "lte": lambda value, threshold: value <= threshold,
}


class SampleStore:
    """Read side of the recorded sample store.

    This class never writes to `samples.jsonl` and has no code path that can
    produce a row the file does not contain. Everything it refuses to serve -
    a row outside the retention window, a row stamped after the world clock, a
    line it could not parse - is counted and reported, because an observability
    store that quietly drops evidence is worse than one that has none.

    Retention is applied here rather than by truncating the file: the writer's
    only permitted operation is append, so history stays intact on disk and the
    window is what the surface will answer for. Whether that window is also
    enforced on disk is the writer's business, not the reader's.
    """

    def __init__(self, path: Path, retention_days: int, now: datetime):
        self.path = Path(path)
        self.retention_days = retention_days
        self.now = now
        self.window_start = now - timedelta(days=retention_days)
        self._read: dict | None = None

    @property
    def present(self) -> bool:
        return self.path.is_file()

    def read(self) -> dict:
        if self._read is not None:
            return self._read
        rows: list[dict] = []
        aged_out = ahead = malformed = 0
        malformed_detail: list[dict] = []
        aged_by_metric: dict[str, int] = {}
        present = self.present
        if present:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError) as exc:
                raise ToolError(
                    "unreadable_samples", f"Cannot read the recorded sample store: {exc}"
                ) from None
            for number_, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                try:
                    row = SampleRow.model_validate(json.loads(line))
                    at = parse_vts(row.vts)
                except Exception as exc:
                    malformed += 1
                    if len(malformed_detail) < MALFORMED_CAP:
                        reason = " ".join(str(exc).split())[:160]
                        malformed_detail.append(
                            {"line": number_, "reason": f"{type(exc).__name__}: {reason}"}
                        )
                    continue
                if at < self.window_start:
                    aged_out += 1
                    aged_by_metric[row.metric] = aged_by_metric.get(row.metric, 0) + 1
                    continue
                if at > self.now:
                    # A row the world has not reached yet is not something this
                    # reader can honestly claim to have observed.
                    ahead += 1
                    continue
                rows.append(
                    {
                        "at": iso(at),
                        "metric": row.metric,
                        "value": row.value,
                        "source": row.source,
                        "run_id": row.run_id,
                        "detail": row.detail,
                    }
                )
        rows.sort(key=lambda item: item["at"])  # stable: append order breaks ties
        self._read = {
            "rows": rows,
            "aged_by_metric": aged_by_metric,
            "status": {
                "store": self.path.name,
                "present": present,
                "retention_days": self.retention_days,
                "window_start": iso(self.window_start),
                "retained": len(rows),
                "aged_out": aged_out,
                "not_yet_observed": ahead,
                "malformed": malformed,
                "malformed_lines": malformed_detail,
                "oldest_retained": rows[0]["at"] if rows else None,
                "newest_retained": rows[-1]["at"] if rows else None,
            },
        }
        return self._read

    @property
    def status(self) -> dict:
        return self.read()["status"]

    def has_history(self, metric_id: str) -> bool:
        return bool(self.for_metric(metric_id))

    def for_metric(self, metric_id: str) -> list[dict]:
        return [
            {key: value for key, value in row.items() if key != "metric"}
            for row in self.read()["rows"]
            if row["metric"] == metric_id
        ]

    def metric_status(self, metric_id: str) -> dict:
        """The store's status narrowed to one metric, so `aged_out` answers the
        question the reader actually asked: how many of *this* metric's samples
        fell outside the window."""
        read = self.read()
        aged = read["aged_by_metric"].get(metric_id, 0)
        kept = sum(1 for row in read["rows"] if row["metric"] == metric_id)
        return {
            **self.status,
            "metric": metric_id,
            "retained": kept,
            "aged_out": aged,
            "aged_out_note": (
                f"{aged} recorded sample(s) for this metric are older than the "
                f"{self.retention_days}-day window and are no longer served."
            )
            if aged
            else None,
        }


class ObservabilityStore:
    def __init__(self, config, warehouse, scheduler, commands, clock):
        self.path: Path = config.observability
        self.samples_path: Path = getattr(
            config, "obs_samples", None
        ) or config.observability.parent / "samples.jsonl"
        self.state_dir: Path = config.obs_state_dir
        self.warehouse = warehouse
        self.scheduler = scheduler
        self.commands = commands
        self.clock = clock

    # ---- definitions -----------------------------------------------------

    def definitions_path(self) -> Path:
        alternate = self.path.parent / ALTERNATE_DEFINITIONS
        if self.path.is_file():
            if alternate.is_file() and alternate != self.path:
                # Two definitions files means the reader and the world's writer
                # can be looking at different metric sets, which is the one way
                # this surface can be wrong without anything looking wrong.
                raise ToolError(
                    "invalid_observability",
                    f"Two definitions files are present ({self.path.name} and "
                    f"{ALTERNATE_DEFINITIONS}); a world must declare exactly one",
                )
            return self.path
        return alternate if alternate.is_file() else self.path

    def spec(self) -> ObservabilitySpec:
        try:
            raw = yaml.safe_load(self.definitions_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ToolError(
                "no_observability", "This world has no metrics, alarms or deploys"
            ) from None
        except (yaml.YAMLError, UnicodeError) as exc:
            raise ToolError("invalid_observability", f"Unreadable definitions: {exc}") from None
        try:
            return ObservabilitySpec.model_validate(raw or {})
        except ValidationError as exc:
            raise ToolError("invalid_observability", f"Invalid definitions: {exc}") from None

    def metric_def(self, spec, name) -> MetricDef:
        for metric in spec.metrics:
            if metric.id == name:
                return metric
        raise ToolError("metric_not_found", f"Unknown metric: {name}")

    def alarm_def(self, spec, name) -> AlarmDef:
        for alarm in spec.alarms:
            if alarm.id == name:
                return alarm
        raise ToolError("alarm_not_found", f"Unknown alarm: {name}")

    def deploy_def(self, spec, name) -> DeployDef:
        for deploy in spec.deploys:
            if deploy.id == name:
                return deploy
        raise ToolError("deploy_not_found", f"Unknown deploy: {name}")

    def samples(self, spec: ObservabilitySpec) -> SampleStore:
        """The world's recorded sample store, windowed by the world's own
        declared retention. One per call, so a single tool call reads the file
        once and every number in the response comes from the same read."""
        return SampleStore(self.samples_path, spec.retention.days, parse_vts(self.clock()))

    # ---- observation record (outside the world tree) ----------------------

    def record(self) -> dict:
        path = self.state_dir / "observations.json"
        if not path.exists():
            return {"schema_version": 1, "samples": {}, "alarms": {}, "deploys": [],
                    "statuses": []}
        written = json.loads(path.read_text(encoding="utf-8"))
        # `statuses` arrived after the first records were written. A default
        # here rather than a migration: a record from before it existed is a
        # record with no statuses in it, which is exactly what it means.
        written.setdefault("statuses", [])
        return written

    def save_record(self, record: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / "observations.json"
        pending = path.with_suffix(".pending")
        pending.write_bytes(canonical(record) + b"\n")
        pending.replace(path)

    # ---- warehouse access -------------------------------------------------

    def read_sql(self, sql: str, observation: Observation, limit: int = 1000):
        """Every warehouse reading a metric takes goes through here, read-only.

        The caller's observation collects the tables that were genuinely read,
        so the action row names them and the process signals can tell that
        consulting a metric was a read of the underlying table."""
        current = Observation()
        result = self.warehouse.execute(sql, current, read_only=True, limit=limit)
        observation.read_targets = sorted(set(observation.read_targets) | set(current.read_targets))
        observation.targets = sorted(set(observation.targets) | set(current.read_targets))
        return result

    @staticmethod
    def cell(result, row=0, column=0):
        rows = result.get("rows") or []
        if len(rows) <= row or len(rows[row]) <= column:
            return None
        return rows[row][column]

    # ---- scheduler access -------------------------------------------------

    def runs(self):
        """Normalize both run shapes: the world builder writes job/vts_start/
        vts_end, the gateway's own trigger writes job_id/started_at/finished_at."""
        state = self.scheduler.state(None)
        result = []
        for run in state.get("runs", []):
            started = run.get("vts_start") or run.get("started_at")
            finished = run.get("vts_end") or run.get("finished_at")
            result.append(
                {
                    "run_id": run.get("id"),
                    "job": run.get("job") or run.get("job_id"),
                    "status": run.get("status"),
                    "started_at": started,
                    "finished_at": finished,
                    "detail": run.get("detail"),
                }
            )
        return result

    def job_runs(self, job: str, as_of: datetime):
        """Runs of one job that had finished by `as_of` - the virtual clock is
        the only time authority, so a run that has not finished yet is not
        history and does not contribute to a duration or an error rate."""
        selected = []
        for run in self.runs():
            if run["job"] != job:
                continue
            finished = moment(run["finished_at"]) if run["finished_at"] else None
            if finished is None or finished > as_of:
                continue
            started = moment(run["started_at"]) if run["started_at"] else None
            selected.append(
                {
                    **run,
                    "at": iso(finished),
                    "duration_s": (finished - started).total_seconds() if started else None,
                }
            )
        selected.sort(key=lambda run: (run["at"], run["run_id"] or ""))
        return selected

    # ---- metric evaluation ------------------------------------------------

    def evaluate(
        self,
        metric: MetricDef,
        as_of: datetime,
        observation: Observation,
        point_in_time: bool = False,
    ) -> dict:
        """Measure `metric` as it stood at `as_of`.

        `point_in_time` is set only when an alarm declares an evaluation lag. It
        asks for a genuine reconstruction of the earlier reading - the watermark
        restricted to rows that existed by then, run history truncated to then -
        rather than the current reading with an older clock subtracted from it.
        At lag zero nothing is restricted, so a row dated after the world clock
        still shows up as negative freshness instead of being filtered away.
        """
        base = {
            "metric": metric.id,
            "kind": metric.kind,
            "unit": metric.unit,
            "title": metric.title,
            "as_of": iso(as_of),
            "point_in_time": point_in_time,
            "value": None,
            "status": "insufficient_data",
            "reason": None,
            "detail": {},
        }
        try:
            reading = getattr(self, f"_{metric.kind}")(metric, as_of, observation, point_in_time)
            return {**base, **reading}
        except ToolError as exc:
            # A metric whose table or column is gone is world state the agent
            # needs to see, not a broken surface. Say so; never report 0.
            if exc.code == "invalid_observability":
                raise
            return {**base, "reason": f"{exc.code}: {exc}"}
        except Exception as exc:  # driver errors stay dialect-agnostic here
            return {**base, "reason": f"{type(exc).__name__}: {exc}"}

    def _freshness(self, metric, as_of, observation, point_in_time=False):
        table = qualified(metric.source.table, "source.table")
        column = qualified(metric.source.timestamp_column, "source.timestamp_column")
        sql = f"SELECT max({column}) AS watermark FROM {table}"
        if point_in_time:
            sql += (
                f" WHERE CAST({column} AS TIMESTAMP) <= CAST('{sql_timestamp(as_of)}' AS TIMESTAMP)"
            )
        result = self.read_sql(sql, observation)
        watermark = moment(self.cell(result))
        if watermark is None:
            return {
                "reason": f"{table}.{column} has no usable maximum value at or before this time"
            }
        return {
            "value": (as_of - watermark).total_seconds(),
            "status": "ok",
            # Negative lag is reported, never clamped: rows dated after the world
            # clock are a real defect and hiding them would hide the defect.
            "detail": {"watermark": iso(watermark), "table": table, "column": column},
        }

    def _row_count(self, metric, as_of, observation, point_in_time=False):
        table = qualified(metric.source.table, "source.table")
        result = self.read_sql(f"SELECT count(*) AS metric_value FROM {table}", observation)
        value = number(self.cell(result))
        if value is None:
            return {"reason": f"count(*) on {table} returned no value"}
        return {"value": value, "status": "ok", "detail": {"table": table}}

    def _null_rate(self, metric, as_of, observation, point_in_time=False):
        table = qualified(metric.source.table, "source.table")
        column = qualified(metric.source.column, "source.column")
        # count(col) skips nulls in every supported dialect; FILTER does not.
        result = self.read_sql(
            f"SELECT count(*) AS total, count({column}) AS present FROM {table}", observation
        )
        total = number(self.cell(result, column=0))
        present = number(self.cell(result, column=1))
        if total is None or present is None:
            return {"reason": f"null accounting on {table}.{column} returned no value"}
        if total == 0:
            return {"reason": f"{table} is empty, so a null rate is undefined"}
        return {
            "value": (total - present) / total,
            "status": "ok",
            "detail": {
                "table": table,
                "column": column,
                "rows": int(total),
                "null_rows": int(total - present),
            },
        }

    def _job_duration(self, metric, as_of, observation, point_in_time=False):
        observation.read_targets = sorted({*observation.read_targets, "scheduler"})
        observation.targets = sorted({*observation.targets, "scheduler"})
        runs = [run for run in self.job_runs(metric.source.job, as_of) if run["duration_s"]]
        if not runs:
            return {"reason": f"job {metric.source.job} has no finished run at or before this time"}
        latest = runs[-1]
        return {
            "value": latest["duration_s"],
            "status": "ok",
            "detail": {
                "job": metric.source.job,
                "run_id": latest["run_id"],
                "started_at": latest["started_at"],
                "finished_at": latest["finished_at"],
                "status_of_run": latest["status"],
            },
        }

    def _job_error_rate(self, metric, as_of, observation, point_in_time=False):
        observation.read_targets = sorted({*observation.read_targets, "scheduler"})
        observation.targets = sorted({*observation.targets, "scheduler"})
        runs = self.job_runs(metric.source.job, as_of)
        window = metric.source.window_runs
        if window:
            runs = runs[-window:]
        if not runs:
            return {"reason": f"job {metric.source.job} has no finished run at or before this time"}
        failures = [run for run in runs if run["status"] != "success"]
        return {
            "value": len(failures) / len(runs),
            "status": "ok",
            "detail": {
                "job": metric.source.job,
                "runs": len(runs),
                "failed_runs": len(failures),
                "window_runs": window,
            },
        }

    def _sql_scalar(self, metric, as_of, observation, point_in_time=False):
        result = self.read_sql(metric.source.sql, observation, limit=2)
        raw = self.cell(result)
        value = number(raw)
        if value is None:
            return {"reason": "the declared query returned no numeric value in its first column"}
        return {
            "value": value,
            "status": "ok",
            # `raw` keeps the exact value (a DECIMAL survives as its digits) so
            # nothing is lost to the float the comparison uses.
            "detail": {"raw": raw, "columns": result.get("columns")},
        }

    # ---- series -----------------------------------------------------------

    def series(
        self,
        metric: MetricDef,
        observation: Observation,
        limit: int = 200,
        store: SampleStore | None = None,
    ) -> dict:
        as_of = parse_vts(self.clock())
        # History comes from the record when the world keeps one. This is the
        # split the whole store exists for: `metrics_get` computes *now* from
        # live state, and *then* is whatever was written down at the time. A
        # derivation run now describes the world as it now stands, which after
        # an advance is a different world - so it is not offered as history.
        if store is not None and store.present:
            recorded = store.for_metric(metric.id)
            if recorded:
                return {
                    "source": "recorded",
                    "points": recorded[-max(limit, SAMPLE_CAP) :],
                    "note": (
                        "Recorded samples, read from the world's observability store as they "
                        "were written at the time. They are not re-derived from current state "
                        "and generally cannot be: the state that produced an earlier value is "
                        "gone once the world advances."
                    ),
                    "recorded": store.metric_status(metric.id),
                }
        if metric.kind in RUN_SERIES_KINDS:
            return self._run_series(metric, as_of, observation, limit)
        if metric.series is None:
            return {
                "source": "none",
                "points": [],
                "note": (
                    f"{metric.kind} has no series derivable from current state; only the "
                    "observed samples below are history."
                ),
            }
        # Same rule as a scalar reading: a series the world cannot supply is an
        # empty series that says why, not a failure that blanks the whole page
        # and not a fabricated shape. A broken *definition* still raises.
        try:
            if metric.series.sql is not None:
                return self._declared_series(metric, observation, limit)
            return self._bucket_series(metric, observation, limit)
        except ToolError as exc:
            if exc.code == "invalid_observability":
                raise
            return {"source": "unavailable", "points": [], "note": f"{exc.code}: {exc}"}
        except Exception as exc:
            return {
                "source": "unavailable",
                "points": [],
                "note": f"{type(exc).__name__}: {exc}",
            }

    def _run_series(self, metric, as_of, observation, limit):
        observation.read_targets = sorted({*observation.read_targets, "scheduler"})
        observation.targets = sorted({*observation.targets, "scheduler"})
        points = []
        for run in self.job_runs(metric.source.job, as_of):
            if metric.kind == "job_duration":
                if run["duration_s"] is None:
                    continue
                value = run["duration_s"]
            else:
                value = 0.0 if run["status"] == "success" else 1.0
            points.append({"at": run["at"], "value": value, "run_id": run["run_id"]})
        return {
            "source": "scheduler_run_history",
            "points": points[-limit:],
            "note": None,
        }

    def _bucket_series(self, metric, observation, limit):
        table = qualified(metric.source.table, "source.table")
        bucket = qualified(metric.series.bucket_column, "series.bucket_column")
        if metric.kind == "row_count":
            sql = f"SELECT {bucket} AS bucket, count(*) AS value FROM {table} GROUP BY 1 ORDER BY 1"
        else:
            column = qualified(metric.source.column, "source.column")
            sql = (
                f"SELECT {bucket} AS bucket, count(*) AS total, count({column}) AS present "
                f"FROM {table} GROUP BY 1 ORDER BY 1"
            )
        result = self.read_sql(sql, observation, limit=10_000)
        points = []
        for row in result.get("rows") or []:
            at = row[0] if isinstance(row[0], str) else None
            if metric.kind == "row_count":
                value = number(row[1])
            else:
                total, present = number(row[1]), number(row[2])
                value = None if not total else (total - present) / total
            if at is None or value is None:
                continue
            points.append({"at": at, "value": value})
        return {
            "source": "derived_from_data",
            "points": points[-limit:],
            "note": (
                "Each point is this metric computed over one bucket of the source table. "
                "The scalar value covers the whole table, so the two differ in scope."
            ),
            "truncated": bool(result.get("truncated")),
        }

    def _declared_series(self, metric, observation, limit):
        result = self.read_sql(metric.series.sql, observation, limit=10_000)
        points = []
        for row in result.get("rows") or []:
            at = row[0] if isinstance(row[0], str) else None
            value = number(row[1]) if len(row) > 1 else None
            if at is None or value is None:
                continue
            points.append({"at": at, "value": value})
        return {
            "source": "declared_sql",
            "points": points[-limit:],
            "note": None,
            "truncated": bool(result.get("truncated")),
        }

    # ---- public metric tools ---------------------------------------------

    def public_metric(self, metric: MetricDef) -> dict:
        return {
            "id": metric.id,
            "kind": metric.kind,
            "unit": metric.unit,
            "title": metric.title,
            "description": metric.description,
            "source": metric.source.model_dump(exclude_none=True),
            "has_series": metric.has_series,
        }

    def sample(self, record, metric_id, reading):
        if reading["status"] != "ok":
            return
        samples = record["samples"].setdefault(metric_id, [])
        point = {"at": reading["as_of"], "value": reading["value"]}
        if samples and samples[-1] == point:
            return
        samples.append(point)
        del samples[:-SAMPLE_CAP]

    def list_metrics(self, observation: Observation) -> dict:
        spec = self.spec()
        observation.targets = observation.read_targets = ["metrics"]
        now = parse_vts(self.clock())
        record = self.record()
        metrics = []
        for metric in spec.metrics:
            reading = self.evaluate(metric, now, observation)
            self.sample(record, metric.id, reading)
            metrics.append({**self.public_metric(metric), "current": reading})
        self.save_record(record)
        return {"evaluated_at": iso(now), "metrics": metrics}

    def get_metric(self, name: str, observation: Observation) -> dict:
        spec = self.spec()
        metric = self.metric_def(spec, name)
        observation.targets = observation.read_targets = [f"metric:{name}"]
        now = parse_vts(self.clock())
        reading = self.evaluate(metric, now, observation)
        record = self.record()
        self.sample(record, metric.id, reading)
        self.save_record(record)
        alarms = [alarm.id for alarm in spec.alarms if alarm.metric == name]
        return {**self.public_metric(metric), "current": reading, "alarms": alarms}

    def get_series(self, name: str, limit: int, observation: Observation) -> dict:
        spec = self.spec()
        metric = self.metric_def(spec, name)
        observation.targets = observation.read_targets = [f"metric:{name}"]
        store = self.samples(spec)
        series = self.series(metric, observation, limit, store=store)
        samples = self.record()["samples"].get(name, [])
        points = series["points"][-limit:] if series["source"] == "recorded" else series["points"]
        return {
            "metric": name,
            "kind": metric.kind,
            "unit": metric.unit,
            "series_source": series["source"],
            "points": points,
            "note": series["note"],
            # Kept apart on purpose: derived points are re-read from the world
            # every time, observed samples are this episode's own readings.
            "observed_samples": samples[-limit:],
            # Always reported, so "there is no recorded history for this metric"
            # and "the sample you want aged out" are different, visible answers
            # rather than the same empty list.
            "recorded": store.metric_status(name),
        }

    # ---- alarm evaluation -------------------------------------------------

    def evaluate_alarm(
        self,
        spec,
        alarm: AlarmDef,
        observation: Observation,
        store: SampleStore | None = None,
    ) -> dict:
        metric = self.metric_def(spec, alarm.metric)
        now = parse_vts(self.clock())
        as_of = now - timedelta(seconds=alarm.evaluation_lag_s)
        result = {
            "alarm": alarm.id,
            "metric": alarm.metric,
            "title": alarm.title,
            "description": alarm.description,
            "runbook": alarm.runbook,
            "severity": alarm.severity,
            "comparison": alarm.comparison,
            "threshold": alarm.threshold,
            "unit": metric.unit,
            "evaluate_on": alarm.evaluate_on,
            "datapoints_to_alarm": alarm.datapoints_to_alarm,
            "evaluation_lag_s": alarm.evaluation_lag_s,
            "evaluated_at": iso(now),
            "as_of": iso(as_of),
            "value": None,
            "datapoints": [],
            "breaching_datapoints": 0,
            "state": "insufficient_data",
            "reason": None,
        }
        breached = BREACH[alarm.comparison]
        if alarm.evaluate_on == "value":
            reading = self.evaluate(
                metric, as_of, observation, point_in_time=alarm.evaluation_lag_s > 0
            )
            result["value"] = reading["value"]
            result["detail"] = reading["detail"]
            if reading["status"] != "ok":
                result["reason"] = reading["reason"]
                return result
            hit = breached(reading["value"], alarm.threshold)
            result["state"] = "firing" if hit else "ok"
            result["breaching_datapoints"] = int(hit)
            return result
        series = self.series(
            metric, observation, limit=max(alarm.datapoints_to_alarm, 1), store=store
        )
        result["series_source"] = series["source"]
        if series["source"] == "unavailable":
            result["reason"] = series["note"]
            return result
        # Lag on a series is genuine: drop every point later than as_of.
        points = [
            point
            for point in series["points"]
            if (moment(point["at"]) is None or moment(point["at"]) <= as_of)
        ]
        window = points[-alarm.datapoints_to_alarm :]
        # A recorded null is "the reading could not be taken then", which is a
        # thing the world genuinely wrote down. It is not a zero and it is not a
        # pass: an alarm whose window contains one does not know its own state.
        blank = [point["at"] for point in window if point["value"] is None]
        if blank:
            result["datapoints"] = [
                {**point, "breached": None if point["value"] is None else breached(
                    point["value"], alarm.threshold
                )}
                for point in window
            ]
            result["reason"] = (
                "the recorded value is null (insufficient data) at "
                + ", ".join(blank[:5])
            )
            return result
        result["datapoints"] = [
            {**point, "breached": breached(point["value"], alarm.threshold)} for point in window
        ]
        result["breaching_datapoints"] = sum(1 for p in result["datapoints"] if p["breached"])
        result["value"] = window[-1]["value"] if window else None
        if len(window) < alarm.datapoints_to_alarm:
            result["reason"] = (
                f"{len(window)} of {alarm.datapoints_to_alarm} datapoints available at or "
                f"before {iso(as_of)}"
            )
            return result
        if result["breaching_datapoints"] == alarm.datapoints_to_alarm:
            result["state"] = "firing"
        elif result["breaching_datapoints"]:
            result["state"] = "pending"
        else:
            result["state"] = "ok"
        return result

    def recorded_transitions(self, alarm: AlarmDef, store: SampleStore | None) -> list[dict]:
        """Replay the alarm's comparison over the *recorded* samples for its
        metric and emit one entry per state change.

        This is the difference between an alarm that has a history and an alarm
        that only has a present. Nothing here is recomputed from the warehouse:
        every state comes from a value the world wrote down at the moment it
        held, so an alarm that fired on Tuesday and cleared on Wednesday still
        shows Tuesday's firing on Thursday - even though nothing in Thursday's
        world would produce it.

        `evaluation_lag_s` deliberately plays no part: a recorded transition is
        stamped with the sample's own `vts`, which is when the value genuinely
        held. Lag describes when a collector would have *noticed*, and applies
        to the current evaluation only."""
        if store is None or not store.present:
            return []
        rows = store.for_metric(alarm.metric)
        if not rows:
            return []
        breached = BREACH[alarm.comparison]
        need = alarm.datapoints_to_alarm if alarm.evaluate_on == "series" else 1
        out: list[dict] = []
        previous = None
        window: list[dict] = []
        for row in rows:
            window.append(row)
            del window[:-need]
            value = row["value"]
            if len(window) < need:
                state = "insufficient_data"
                note = f"{len(window)} of {need} recorded datapoints"
            elif any(point["value"] is None for point in window):
                state = "insufficient_data"
                note = "a recorded value in the window is null"
            else:
                hits = sum(1 for point in window if breached(point["value"], alarm.threshold))
                state = "firing" if hits == need else ("ok" if hits == 0 else "pending")
                note = None
            if state == previous:
                continue
            previous = state
            out.append(
                {
                    "at": row["at"],
                    "state": state,
                    "value": value,
                    "note": note,
                    "origin": "recorded_samples",
                    "source": row["source"],
                    "run_id": row["run_id"],
                }
            )
        return out

    def history_for(self, record, alarm: AlarmDef, store: SampleStore | None = None) -> list[dict]:
        seeded = [
            {**item.model_dump(exclude_none=False), "origin": "world_seed"}
            for item in alarm.history
        ]
        observed = record["alarms"].get(alarm.id, [])
        recorded = self.recorded_transitions(alarm, store)
        return sorted(seeded + recorded + observed, key=lambda item: item["at"])

    def note_transition(
        self, record, alarm: AlarmDef, result: dict, store: SampleStore | None = None
    ) -> list[dict]:
        history = self.history_for(record, alarm, store)
        if not history or history[-1]["state"] != result["state"]:
            entry = {
                "at": result["evaluated_at"],
                "state": result["state"],
                "value": result["value"],
                "note": result["reason"],
            }
            record["alarms"].setdefault(alarm.id, []).append(
                {**entry, "origin": "gateway_observed"}
            )
            history = history + [{**entry, "origin": "gateway_observed"}]
        return history

    @staticmethod
    def since_from(history: list[dict], state: str, fallback: str) -> str:
        """Walk back from the end while the state holds. The first entry that
        disagrees ends the run, so a flapping alarm reports the start of the
        *current* episode rather than the first time it was ever in this state."""
        since = None
        for item in reversed(history):
            if item["state"] != state:
                break
            since = item["at"]
        return since or fallback

    def recorded_summary(self, alarm: AlarmDef, store: SampleStore | None) -> dict:
        """What the record says about this alarm, kept apart from what the
        current evaluation says. `fired_in_window` is the field the store exists
        for: it answers "has this ever fired inside retention" from evidence,
        and no amount of reading present state can answer it."""
        if store is None or not store.present:
            return {"present": False}
        transitions = self.recorded_transitions(alarm, store)
        fired = [item for item in transitions if item["state"] == "firing"]
        status = store.metric_status(alarm.metric)
        return {
            "present": True,
            "metric": alarm.metric,
            "samples": status["retained"],
            "aged_out": status["aged_out"],
            "retention_days": status["retention_days"],
            "window_start": status["window_start"],
            "transitions": len(transitions),
            "fired_in_window": bool(fired),
            "last_fired_at": fired[-1]["at"] if fired else None,
            "last_recorded_state": transitions[-1]["state"] if transitions else None,
            "last_recorded_at": transitions[-1]["at"] if transitions else None,
        }

    def with_history(
        self, record, alarm: AlarmDef, result: dict, store: SampleStore | None = None
    ) -> dict:
        history = self.note_transition(record, alarm, result, store)
        return {
            **result,
            "since": self.since_from(history, result["state"], result["evaluated_at"]),
            "recorded": self.recorded_summary(alarm, store),
        }

    def list_alarms(self, state_filter, observation: Observation) -> dict:
        spec = self.spec()
        observation.targets = observation.read_targets = ["alarms"]
        record = self.record()
        store = self.samples(spec)
        alarms = []
        for alarm in spec.alarms:
            result = self.with_history(
                record, alarm, self.evaluate_alarm(spec, alarm, observation, store), store
            )
            if state_filter is None or result["state"] == state_filter:
                alarms.append(result)
        self.save_record(record)
        return {
            "evaluated_at": self.clock(),
            "firing": sum(1 for alarm in alarms if alarm["state"] == "firing"),
            # Firing *now* and having fired inside the retention window are
            # different questions, and only the record can answer the second.
            "fired_in_window": sum(
                1 for alarm in alarms if alarm["recorded"].get("fired_in_window")
            ),
            "samples": store.status,
            "alarms": alarms,
        }

    def get_alarm(self, name: str, observation: Observation) -> dict:
        spec = self.spec()
        alarm = self.alarm_def(spec, name)
        observation.targets = observation.read_targets = [f"alarm:{name}"]
        record = self.record()
        store = self.samples(spec)
        result = self.with_history(
            record, alarm, self.evaluate_alarm(spec, alarm, observation, store), store
        )
        history = self.history_for(record, alarm, store)
        self.save_record(record)
        return {**result, "history": history}

    def alarm_history(self, name, limit: int, observation: Observation) -> dict:
        spec = self.spec()
        record = self.record()
        alarms = [self.alarm_def(spec, name)] if name else spec.alarms
        observation.targets = observation.read_targets = [f"alarm:{name}" if name else "alarms"]
        store = self.samples(spec)
        return {
            "history": {
                alarm.id: self.history_for(record, alarm, store)[-limit:] for alarm in alarms
            },
            # Named so an empty history is readable: no record at all, a record
            # with nothing for this metric, or a record whose relevant rows have
            # aged out are three different situations.
            "samples": store.status,
        }

    # ---- silences ---------------------------------------------------------
    #
    # A silence is the one thing an operator does TO the alarm surface, and
    # until now no transport could do it: the 63-tool list has `alarms_list`,
    # `alarms_get` and `alarms_history` and no write, so the native
    # Alertmanager kept its silences in the service process - audited, but
    # invisible to the world fingerprint and gone on restart
    # (SERVICES_B_REQUESTS.md item 2).
    #
    # They live in `var/obs/silences.json`, beside the definitions and inside
    # the world tree, NOT in the observation record next door. The record holds
    # what the gateway *observed* while reading (samples, alarm transitions,
    # deploy history) and is deliberately outside the world so that observing
    # cannot perturb Track A's fingerprint. A silence is not an observation: it
    # changes what the surface reports to everyone who reads it afterwards, so
    # it is world state, and a grader's world diff has to be able to see it.

    def silences_path(self) -> Path:
        return self.path.parent / SILENCES_FILE

    def silences(self) -> list[dict]:
        """Every silence written into this world, oldest first."""
        try:
            raw = json.loads(self.silences_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (ValueError, UnicodeError) as exc:
            raise ToolError(
                "invalid_observability", f"{SILENCES_FILE} is not readable: {exc}"
            ) from None
        return list(raw.get("silences", []))

    def active_silences(self, at: str | None = None) -> dict[str, dict]:
        """-> the silence in force over each alarm at a virtual timestamp.

        Latest wins when two silences cover one alarm, which is what re-acking
        an alarm with a longer window means.
        """
        now = parse_vts(at or self.clock())
        in_force: dict[str, dict] = {}
        for entry in self.silences():
            if parse_vts(entry["at"]) <= now < parse_vts(entry["until"]):
                for alarm in entry["alarms"]:
                    in_force[alarm] = entry
        return in_force

    def list_silences(self, observation: Observation) -> dict:
        """Read the silence log, with each entry's state at the world clock."""
        self.spec()  # a world with no observability has no silence surface at all
        observation.targets = observation.read_targets = ["alarms"]
        now = parse_vts(self.clock())
        return {
            "evaluated_at": self.clock(),
            "silences": [
                {**entry,
                 "state": "active"
                 if parse_vts(entry["at"]) <= now < parse_vts(entry["until"])
                 else "expired"}
                for entry in self.silences()
            ],
        }

    def silence(self, alarms, until: str, comment, actor, observation: Observation) -> dict:
        """Silence one or more declared alarms until a virtual timestamp.

        One entry covering several alarms rather than one entry each, because
        that is what an operator does - "not this class of page until the
        backfill lands" - and because it keeps one action on one row, whose
        `targets` name every alarm it covers.

        Every alarm is resolved against the spec first, so a typo is a refusal
        and not a silence that quietly covers nothing.
        """
        spec = self.spec()
        wanted = sorted({name for name in alarms})
        if not wanted:
            raise ToolError("invalid_silence", "Name at least one alarm to silence")
        for name in wanted:
            self.alarm_def(spec, name)
        now = self.clock()
        if parse_vts(until) <= parse_vts(now):
            raise ToolError(
                "invalid_silence",
                f"A silence has to end after the world clock ({now}); {until} is not later",
            )
        observation.op_class = "write"
        observation.targets = [f"alarm:{name}" for name in wanted]
        entry = {
            "alarms": wanted,
            "at": now,
            "until": until,
            "comment": comment or "",
            "actor": actor or "agent",
        }
        # The id is derived from the entry, the way deploy ids are derived from
        # the history entry they name: the id a write returns is the id a later
        # read returns, in this process or another, with no counter to keep.
        entry = {"id": digest(entry)[:16], **entry}
        entries = self.silences() + [entry]
        path = self.silences_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_suffix(".pending")
        pending.write_bytes(canonical({"schema_version": 1, "silences": entries}) + b"\n")
        pending.replace(path)
        return entry

    def expire_silence(self, silence_id: str, observation: Observation) -> dict:
        """End a silence now. Without this a silence written too wide is
        permanent, which is a worse lie than one that does not outlive a process.

        Rewrites the entry rather than appending a cancellation, because the
        question every reader asks is "is this alarm silenced now" and answering
        it from a log of edits is how two readers come to disagree.
        """
        self.spec()
        entries = self.silences()
        for entry in entries:
            if entry["id"] == silence_id:
                break
        else:
            raise ToolError("silence_not_found", f"Unknown silence: {silence_id}")
        observation.op_class = "write"
        observation.targets = [f"alarm:{name}" for name in entry["alarms"]]
        entry["until"] = min(entry["until"], self.clock())
        path = self.silences_path()
        pending = path.with_suffix(".pending")
        pending.write_bytes(canonical({"schema_version": 1, "silences": entries}) + b"\n")
        pending.replace(path)
        return entry

    # ---- deploys ----------------------------------------------------------

    def snapshot(self, spec, deploy: DeployDef, observation: Observation, record) -> dict:
        names = sorted({*deploy.canary.metrics, *(g.metric for g in deploy.canary.guards)})
        now = parse_vts(self.clock())
        metrics = {}
        for name in names:
            reading = self.evaluate(self.metric_def(spec, name), now, observation)
            self.sample(record, name, reading)
            metrics[name] = reading
        alarms = {}
        for name in deploy.canary.alarms:
            alarm = self.alarm_def(spec, name)
            # The canary is the one place that must NOT read the record: no
            # store row is written by a gateway deploy, so a series alarm read
            # from the record would return the same answer before and after and
            # could not see the deploy at all. A canary asks "what is true now",
            # twice - which is exactly the computed half of the split.
            alarms[name] = self.with_history(
                record, alarm, self.evaluate_alarm(spec, alarm, observation, store=None)
            )
        return {"at": iso(now), "metrics": metrics, "alarms": alarms}

    @staticmethod
    def check_guard(guard: Guard, before: dict, after: dict) -> dict:
        bounds = {
            name: getattr(guard, name)
            for name in (
                "max_increase_pct",
                "max_decrease_pct",
                "max_increase_abs",
                "max_decrease_abs",
            )
            if getattr(guard, name) is not None
        }
        result = {"metric": guard.metric, "bounds": bounds, "breached": False, "reason": None}
        if before["status"] != "ok" or after["status"] != "ok":
            result["reason"] = "a reading was unavailable before or after the deploy"
            return result
        delta = after["value"] - before["value"]
        breaches = []
        if "max_increase_abs" in bounds and delta > bounds["max_increase_abs"]:
            breaches.append(f"rose by {delta:g} (limit {bounds['max_increase_abs']:g})")
        if "max_decrease_abs" in bounds and -delta > bounds["max_decrease_abs"]:
            breaches.append(f"fell by {-delta:g} (limit {bounds['max_decrease_abs']:g})")
        if before["value"]:
            pct = delta / abs(before["value"]) * 100
            if "max_increase_pct" in bounds and pct > bounds["max_increase_pct"]:
                breaches.append(f"rose {pct:.2f}% (limit {bounds['max_increase_pct']:g}%)")
            if "max_decrease_pct" in bounds and -pct > bounds["max_decrease_pct"]:
                breaches.append(f"fell {-pct:.2f}% (limit {bounds['max_decrease_pct']:g}%)")
        elif {"max_increase_pct", "max_decrease_pct"} & set(bounds):
            result["reason"] = "the value before the deploy was zero, so no percentage applies"
        result["breached"] = bool(breaches)
        if breaches:
            result["reason"] = "; ".join(breaches)
        return result

    def compare(self, deploy: DeployDef, before: dict, after: dict) -> dict:
        metrics = []
        for name in sorted(before["metrics"]):
            was, now = before["metrics"][name], after["metrics"][name]
            delta = (
                now["value"] - was["value"]
                if was["status"] == "ok" and now["status"] == "ok"
                else None
            )
            metrics.append(
                {
                    "metric": name,
                    "unit": was["unit"],
                    "before": was["value"],
                    "after": now["value"],
                    "before_status": was["status"],
                    "after_status": now["status"],
                    "delta": delta,
                    "delta_pct": (
                        delta / abs(was["value"]) * 100
                        if delta is not None and was["value"]
                        else None
                    ),
                }
            )
        guards = [
            self.check_guard(guard, before["metrics"][guard.metric], after["metrics"][guard.metric])
            for guard in deploy.canary.guards
        ]
        states = {name: value["state"] for name, value in before["alarms"].items()}
        now_states = {name: value["state"] for name, value in after["alarms"].items()}
        newly_firing = sorted(
            name
            for name, state in now_states.items()
            if state == "firing" and states.get(name) != "firing"
        )
        cleared = sorted(
            name
            for name, state in states.items()
            if state == "firing" and now_states.get(name) != "firing"
        )
        still_firing = sorted(
            name
            for name, state in now_states.items()
            if state == "firing" and states.get(name) == "firing"
        )
        unknown = any(entry["after_status"] != "ok" for entry in metrics) or any(
            state == "insufficient_data" for state in now_states.values()
        )
        breached = any(guard["breached"] for guard in guards) or bool(newly_firing)
        status = "breached" if breached else ("insufficient_data" if unknown else "clean")
        return {
            "status": status,
            "metrics": metrics,
            "guards": guards,
            "alarms": {
                "before": states,
                "after": now_states,
                "newly_firing": newly_firing,
                "cleared": cleared,
                "still_firing": still_firing,
            },
        }

    def public_deploy(self, deploy: DeployDef) -> dict:
        return {
            "id": deploy.id,
            "kind": deploy.kind,
            "title": deploy.title,
            "description": deploy.description,
            "selector": deploy.selector,
            "statements": deploy.statements,
            "canary": deploy.canary.model_dump(exclude_none=True),
        }

    def list_deploys(self, observation: Observation) -> dict:
        spec = self.spec()
        observation.targets = observation.read_targets = ["deploys"]
        return {"deploys": [self.public_deploy(deploy) for deploy in spec.deploys]}

    def run_deploy(self, name, full_refresh, variables, observation: Observation) -> dict:
        spec = self.spec()
        deploy = self.deploy_def(spec, name)
        record = self.record()
        started = self.clock()
        before = self.snapshot(spec, deploy, observation, record)
        targets = {f"deploy:{name}", *observation.targets}
        if deploy.kind == "dbt":
            # A deploy is at least a write. A full refresh drops and rebuilds the
            # models, which replaces state, so it classifies destructive - and the
            # process signals will flag it when the agent never read the target.
            observation.op_class = "destructive" if full_refresh else "write"
            if full_refresh:
                observation.flags.append("full_refresh")
            targets.add("dbt_project")
            command = self.commands.dbt(
                "run", deploy.selector or "*", variables, full_refresh, Observation()
            )
        else:
            applied = Observation()
            # One atomic transaction, exactly like sql_execute: a statement that
            # fails rolls the whole deploy back and raises, so there is no
            # after-state to compare and the tool reports the failure instead.
            result = self.warehouse.execute(";\n".join(deploy.statements), applied)
            observation.flags.extend(applied.flags)
            observation.rows_affected = applied.rows_affected
            targets.update(applied.targets)
            order = {"read": 0, "write": 1, "ddl": 2, "destructive": 3}
            observation.op_class = max(
                applied.op_class if applied.op_class in order else "write",
                "write",
                key=order.get,
            )
            command = {"returncode": 0, "statements": len(deploy.statements), "result": result}
        after = self.snapshot(spec, deploy, observation, record)
        observation.targets = sorted(targets)
        canary = self.compare(deploy, before, after)
        entry = {
            "deploy": name,
            "kind": deploy.kind,
            "started_at": started,
            "finished_at": self.clock(),
            "full_refresh": bool(full_refresh),
            # Top level on purpose: `Gateway._call` reads `returncode`/`timed_out`
            # from the result root, so a failed deploy reports exit "error" the
            # way dbt_run does while still handing back the whole canary.
            "returncode": command.get("returncode"),
            "timed_out": bool(command.get("timed_out")),
            "canary_status": canary["status"],
            "newly_firing": canary["alarms"]["newly_firing"],
            "cleared": canary["alarms"]["cleared"],
        }
        record["deploys"].append(entry)
        self.save_record(record)
        return {
            **entry,
            "command": command,
            "canary": canary,
            "before": before,
            "after": after,
            # Ground rule 5: destructive operations are flagged, not blocked.
            "blocked": False,
            "note": (
                "The canary reports; it never blocks. Whether to roll back on a breach "
                "is the operator's call."
            ),
        }

    def deploy_history(self, name, limit: int, observation: Observation) -> dict:
        observation.targets = observation.read_targets = [f"deploy:{name}" if name else "deploys"]
        if name:
            self.deploy_def(self.spec(), name)
        entries = [
            entry for entry in self.record()["deploys"] if name is None or entry["deploy"] == name
        ]
        return {"deploys": entries[-limit:]}

    # An operator's verdict on a deploy: "the canary said clean but the numbers
    # are wrong, treat this as failed". It goes in the observation record beside
    # the deploy history it annotates, and NOT in the world tree the way a
    # silence does, because it changes nothing any other reader of the world
    # sees - it is a note about what was observed, which is what that record is.

    def deploy_status(self, name, state: str, description, actor,
                      observation: Observation) -> dict:
        """Record a verdict on a declared deploy."""
        self.deploy_def(self.spec(), name)
        if not state or not isinstance(state, str):
            raise ToolError("invalid_status", "A deployment status needs a state")
        observation.op_class = "write"
        observation.targets = [f"deploy:{name}"]
        entry = {
            "deploy": name,
            "state": state,
            "description": description or "",
            "actor": actor or "agent",
            "at": self.clock(),
        }
        record = self.record()
        record["statuses"].append(entry)
        self.save_record(record)
        return entry

    def deploy_statuses(self, name, limit: int, observation: Observation) -> dict:
        """The verdicts recorded against one deploy, or against every deploy."""
        observation.targets = observation.read_targets = [f"deploy:{name}" if name else "deploys"]
        if name:
            self.deploy_def(self.spec(), name)
        entries = [
            entry for entry in self.record()["statuses"]
            if name is None or entry["deploy"] == name
        ]
        return {"statuses": entries[-limit:]}
