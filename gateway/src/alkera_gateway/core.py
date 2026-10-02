import json
import logging
import time
from threading import RLock

import duckdb
import psutil
from pydantic import ValidationError

from .audit import AuditLog
from .config import GatewayConfig
from .operations import Operations
from .records import Observation, ToolError, canonical, digest, json_value
from .warehouses import build_tools

logger = logging.getLogger(__name__)


class Gateway:
    def __init__(self, config: GatewayConfig, *, clock=None, scheduler=None):
        if config.initial_world_fp is None and config.world_root is not None:
            try:
                from world.fingerprint import semantic_hash
            except ImportError as exc:
                raise ValueError("Track A fingerprint module is unavailable") from exc
            config = config.model_copy(
                update={"initial_world_fp": semantic_hash(config.world_root)}
            )
        self.config = config
        # The tool table this episode serves. With one warehouse this IS
        # `tools.TOOLS`, the same object, so a later runtime registration
        # (container_shim.register_bash) still reaches the served table.
        # A `config.profile` serves a filtered copy (warehouses.build_tools).
        self.tools = build_tools(config)
        self.clock = clock or self._virtual_time
        self.audit = AuditLog(config)
        self.operations = Operations(config, self.clock, scheduler)
        self.lock = RLock()
        self.healthy = True
        self._disk_sample = None
        self._disk_sample_at = float("-inf")

    def _virtual_time(self):
        try:
            state = json.loads(self.config.scheduler.read_text(encoding="utf-8"))
            return state.get("vnow", self.config.virtual_time)
        except (OSError, ValueError):
            return self.config.virtual_time

    def _disk_mb(self):
        # Disk usage is telemetry, not world evidence. Rewalking a large world
        # on every read can cost more than the operation being measured.
        now = time.monotonic()
        if now - self._disk_sample_at < 5:
            return self._disk_sample
        self._disk_sample = self._measure_disk_mb()
        self._disk_sample_at = now
        return self._disk_sample

    def _measure_disk_mb(self):
        paths = [
            self.config.workspace,
            self.config.warehouse,
            self.config.scheduler,
            self.config.dashboards,
        ]
        total = 0
        try:
            for path in paths:
                if path.is_file():
                    total += path.stat().st_size
                elif path.is_dir():
                    total += sum(
                        item.stat().st_size
                        for item in path.rglob("*")
                        if item.is_file() and not item.is_symlink()
                    )
            return round(total / 1_048_576, 3)
        except OSError:
            return None

    def close(self):
        with self.lock:
            self.healthy = False
            self.audit.close()

    def call(self, tool: str, args: dict, transport: str = "mcp") -> dict:
        with self.lock:
            if not self.healthy:
                raise RuntimeError("Gateway is closed or its audit writer failed")
            return self._call(tool, args, transport)

    def _call(self, tool, args, transport):
        started = time.monotonic()
        request = {
            "seq": self.audit.seq + 1,
            "vts": self.clock(),
            "wts": time.time_ns() // 1_000_000,
            "transport": transport,
            "tool": tool,
            "args": args,
        }
        try:
            self.audit.begin(request)
        except Exception:
            self.healthy = False
            raise
        observation = Observation()
        error = None
        result = None
        normalized_args = args
        try:
            if tool not in self.tools:
                raise ToolError("unknown_tool", f"Unknown tool: {tool}")
            if len(canonical(args)) > self.config.max_payload_bytes:
                raise ToolError("request_too_large", "Request exceeds the configured payload limit")
            model, _ = self.tools[tool]
            normalized_args = model.model_validate(args).model_dump()
            result = getattr(self.operations, tool)(**normalized_args, observation=observation)
            result = json_value(result)
            if len(canonical(result)) > self.config.max_payload_bytes:
                raise ToolError("result_too_large", "Result exceeds the limit; narrow the query")
            if isinstance(result, dict) and (
                result.get("returncode", 0) != 0 or result.get("timed_out")
            ):
                error = {"code": "command_failed", "message": "Command failed; inspect the output"}
        except ToolError as exc:
            error = {"code": exc.code, "message": str(exc)}
            result = None
        except ValidationError as exc:
            error = {"code": "invalid_arguments", "message": str(exc)}
        except (OSError, UnicodeError, duckdb.Error, ValueError) as exc:
            error = {"code": type(exc).__name__, "message": str(exc)}
        except Exception:
            logger.exception("Tool failed: %s, call %s", tool, request["seq"])
            error = {
                "code": "internal_error",
                "message": "Unexpected connector failure; see server log",
            }
        encoded = canonical(result)
        row = {
            **request,
            "schema_version": 1,
            "args": normalized_args,
            "args_hash": digest(normalized_args),
            "args_summary": canonical(normalized_args).decode("utf-8")[:240],
            "op_class": observation.op_class,
            "targets": observation.targets,
            "target": observation.targets[0] if observation.targets else None,
            "read_targets": observation.read_targets,
            "flags": observation.flags,
            "rows_affected": observation.rows_affected,
            "result": result,
            "result_hash": digest(result),
            "result_bytes": len(encoded),
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "exit": "error" if error else "ok",
            "error": error,
            "rss_mb": round(psutil.Process().memory_info().rss / 1_048_576, 3),
            "disk_mb": self._disk_mb(),
        }
        try:
            self.audit.finish(row)
        except Exception:
            self.healthy = False
            raise
        return {
            "ok": error is None,
            "evidence_id": f"call-{row['seq']:06d}",
            "result": result,
            "error": error,
        }

    def checkpoint(self, world_fp: str):
        with self.lock:
            self.audit.append(
                self.audit.directory / "checkpoints.jsonl",
                {
                    "after_seq": self.audit.seq,
                    "vts": self.clock(),
                    "world_fp": world_fp,
                },
            )
