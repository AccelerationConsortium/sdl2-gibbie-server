"""The Gibbie UI bridge (`user-interface/server/app.py` in sdl2_sampleprep_platform).

It runs on the Gibbie PC on 127.0.0.1:8000 and is the one process that knows
whether `master_gibbie_V1.py` is executing. Two GETs, both read-only:
`/api/health` (bridge + backend import) and `/api/status` (run state).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar

import httpx
from sdl_lab_contract import ComponentStatus, ErrorInfo

from .base import Observation, Probe

_TIMEOUT = 2.0


class UiBridgeProbe(Probe):
    probe_type: ClassVar[str] = "ui_bridge"
    primary_operation: ClassVar[str] = (
        "a Gibbie workflow run executing in the UI bridge's worker thread"
    )

    def __init__(self, device_id, cfg, *, transport: httpx.BaseTransport | None = None) -> None:
        super().__init__(device_id, cfg)
        self.url = str(cfg.option("url", "http://127.0.0.1:8000")).rstrip("/")
        self._transport = transport

    @property
    def target(self) -> str:
        return self.url

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.url, timeout=_TIMEOUT, transport=self._transport)

    def probe(self) -> Observation:
        try:
            with self._client() as c:
                health = c.get("/api/health").raise_for_status().json()
                status = c.get("/api/status").raise_for_status().json()
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            return Observation.unreachable(f"UI bridge not answering at {self.url}: {exc}")
        except httpx.HTTPStatusError as exc:
            # The bridge answered but is broken: reachable, and a fault it reported.
            return Observation(
                reachable=True,
                state="error",
                activity="unknown",
                message=f"UI bridge returned HTTP {exc.response.status_code}",
                components={"ui_bridge": ComponentStatus(connected=True, state="http_error")},
            )
        except ValueError as exc:
            return Observation(
                reachable=True, state="error", activity="unknown",
                message=f"UI bridge returned non-JSON: {exc}",
                components={"ui_bridge": ComponentStatus(connected=True, state="bad_response")},
            )
        return self.interpret(health, status)

    @staticmethod
    def interpret(health: dict[str, Any], status: dict[str, Any]) -> Observation:
        """Map the bridge's `run_state` -- {running, simulation, sequence, stages,
        error, started_at, finished_at} -- onto the contract."""

        run_state = status.get("run_state") if isinstance(status.get("run_state"), dict) else {}
        running = bool(run_state.get("running", health.get("running", False)))
        simulation = bool(run_state.get("simulation", False))
        backend_loaded = bool(health.get("backend_loaded", True))
        stages = run_state.get("stages") if isinstance(run_state.get("stages"), dict) else {}
        current_stage = next((tok for tok, st in stages.items() if st == "running"), None)
        done = sum(1 for st in stages.values() if st == "done")
        run_error = run_state.get("error")

        components = {
            "ui_bridge": ComponentStatus(connected=True, state="up"),
            "workflow_backend": ComponentStatus(
                connected=backend_loaded,
                state="loaded" if backend_loaded else "import_failed",
                message=health.get("backend_error"),
            ),
        }
        details: dict[str, Any] = {
            "run_state": run_state,
            "current_stage": current_stage,
            "stages_done": done,
            "stages_total": len(stages),
            "last_warnings": status.get("last_warnings"),
            "config_path": health.get("config_path"),
        }
        progress = f" ({done}/{len(stages)} stages done)" if stages else ""
        if running:
            msg = f"Gibbie run in progress{': ' + current_stage if current_stage else ''}{progress}"
            if simulation:
                # A simulated run is not lab work; `dry_run` accepts any activity (§2.3).
                return Observation(True, "dry_run", "running", "[SIMULATION] " + msg, components, details=details)
            return Observation(True, "busy", "running", msg, components, details=details)
        if not backend_loaded:
            # The cockpit is up but cannot run the workflow: a useful subset (§2.2).
            return Observation(
                True, "degraded", "idle",
                f"UI bridge up but workflow backend failed to import: {health.get('backend_error')}",
                components, details=details,
            )
        if run_error:
            # The bridge holds the last run's failure until the next run starts.
            return Observation(
                True, "error", "idle", f"Last Gibbie run failed: {run_error}", components, details=details,
                last_error=ErrorInfo(code="workflow_failed", message=str(run_error), severity="error",
                                     timestamp=_parse_ts(run_state.get("finished_at"))),
            )
        return Observation(True, "ready", "idle", "Idle; UI bridge up, workflow backend loaded", components, details=details)


def _parse_ts(raw: Any) -> datetime:
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)
