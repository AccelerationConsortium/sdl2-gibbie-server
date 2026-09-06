"""UR arm via the Dashboard Server (TCP 29999, plain text).

Only queries are sent — `robotmode`, `safetystatus`, `programState`,
`get loaded program` — never `play`, `power on`, `brake release` or anything
that changes state. This is also why the probe does not use RTDE: the ur_rtde
control interface uploads and runs a program on the controller the moment it
connects, which would seize the arm from the Gibbie workflow.

Dashboard answers look like ``Robotmode: RUNNING``, ``Safetystatus: NORMAL``,
``PLAYING rtde_control.urp``. Older CB3 controllers answer ``safetymode``
rather than ``safetystatus``; the probe tries the newer command first and falls
back, recording which one answered in ``details.safety_source``.
"""

from __future__ import annotations

import socket
from typing import Callable, ClassVar

from sdl_lab_contract import ComponentStatus, ErrorInfo
from datetime import datetime, timezone

from .base import Observation, Probe

_TIMEOUT = 2.0

_ESTOP = {"ROBOT_EMERGENCY_STOP", "SYSTEM_EMERGENCY_STOP"}
_FAULT = {
    "PROTECTIVE_STOP", "SAFEGUARD_STOP", "AUTOMATIC_MODE_SAFEGUARD_STOP",
    "SYSTEM_THREE_POSITION_ENABLING_STOP", "RECOVERY", "VIOLATION", "FAULT", "UNDEFINED_SAFETY_MODE",
}
_NOT_READY_MODES = {"POWER_OFF", "POWER_ON", "IDLE", "BOOTING", "CONFIRM_SAFETY", "BACKDRIVE"}
_CONTROLLER_LOST = {"NO_CONTROLLER", "DISCONNECTED"}


class DashboardClient:
    """Minimal read-only Dashboard Server session."""

    def __init__(self, host: str, port: int, timeout: float = _TIMEOUT) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: socket.socket | None = None

    def __enter__(self) -> "DashboardClient":
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._file = self._sock.makefile("rwb", buffering=0)
        self.banner = self._readline()  # "Connected: Universal Robots Dashboard Server"
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._sock is not None:
                self._sock.close()
        finally:
            self._sock = None

    def _readline(self) -> str:
        return self._file.readline().decode("utf-8", errors="replace").strip()

    def query(self, command: str) -> str:
        self._file.write((command + "\n").encode())
        return self._readline()


def _value(reply: str) -> str:
    # "Robotmode: RUNNING" -> "RUNNING"; "PLAYING foo.urp" -> "PLAYING foo.urp"
    return reply.split(":", 1)[1].strip() if ":" in reply else reply.strip()


class UrDashboardProbe(Probe):
    probe_type: ClassVar[str] = "ur_dashboard"
    primary_operation: ClassVar[str] = (
        "a program playing on the controller (for Gibbie this is the RTDE control "
        "program the workflow uploads; it plays for the whole session, moving or not)"
    )

    def __init__(self, device_id, cfg, *, client_factory: Callable[..., DashboardClient] | None = None) -> None:
        super().__init__(device_id, cfg)
        # Lab-switch address of Gibbie's UR-3e (the workflow's ROBOT_IP); the
        # older 192.168.1.100 default pointed at a different bench's segment.
        self.host = str(cfg.option("host", "192.168.254.89"))
        self.port = int(cfg.option("port", 29999))
        self._factory = client_factory or DashboardClient

    @property
    def target(self) -> str:
        return f"{self.host}:{self.port}"

    def probe(self) -> Observation:
        try:
            with self._factory(self.host, self.port) as dash:
                robotmode = _value(dash.query("robotmode"))
                safety_reply = dash.query("safetystatus")
                safety_source = "safetystatus"
                if not safety_reply.lower().startswith("safetystatus"):
                    # Older controllers (CB3 / PolyScope 3.x) answer `safetymode`
                    # instead and reject `safetystatus`. The value vocabulary
                    # overlaps, so interpret() handles either.
                    safety_reply = dash.query("safetymode")
                    safety_source = "safetymode"
                safety = _value(safety_reply)
                program = dash.query("programState")
                loaded = dash.query("get loaded program")
        except (OSError, socket.timeout) as exc:
            return Observation.unreachable(f"UR dashboard server not answering at {self.target}: {exc}")
        return self.interpret(robotmode, safety, program, loaded, safety_source=safety_source)

    @staticmethod
    def interpret(
        robotmode: str, safety: str, program: str, loaded: str, *, safety_source: str = "safetystatus"
    ) -> Observation:
        prog_state = program.split(" ", 1)[0].upper() if program else ""
        prog_name = program.split(" ", 1)[1] if " " in program else None
        loaded_name = _value(loaded) if loaded.lower().startswith("loaded program") else None

        components = {
            "controller": ComponentStatus(connected=robotmode not in _CONTROLLER_LOST, state=robotmode.lower() or "unknown"),
            "safety": ComponentStatus(connected=True, state=safety.lower() or "unknown"),
            "program": ComponentStatus(connected=prog_state == "PLAYING", state=prog_state.lower() or "unknown", message=prog_name or loaded_name),
        }
        details = {
            "robotmode": robotmode, "safetystatus": safety, "safety_source": safety_source,
            "program_state": program, "loaded_program": loaded_name,
        }
        now = datetime.now(timezone.utc)

        if safety in _ESTOP:
            return Observation(True, "e_stop", "idle", f"Emergency stop: {safety}", components, details=details,
                               last_error=ErrorInfo(code="ur_estop", message=safety, severity="critical", timestamp=now))
        if safety in _FAULT:
            return Observation(True, "error", "idle", f"Safety stop: {safety}", components, details=details,
                               last_error=ErrorInfo(code="ur_safety_stop", message=safety, severity="error", timestamp=now))
        if robotmode in _CONTROLLER_LOST:
            return Observation(True, "error", "unknown", f"Controller reports {robotmode}", components, details=details,
                               last_error=ErrorInfo(code="ur_controller_lost", message=robotmode, severity="error", timestamp=now))
        if robotmode in _NOT_READY_MODES:
            return Observation(True, "requires_init", "idle", f"Arm not ready: robotmode {robotmode}", components, details=details)
        if robotmode == "RUNNING":
            if prog_state == "PLAYING":
                return Observation(True, "busy", "running", f"Program playing: {prog_name or loaded_name or '?'}", components, details=details)
            if prog_state == "PAUSED":
                return Observation(True, "degraded", "idle", f"Program paused: {prog_name or '?'}", components, details=details)
            return Observation(True, "ready", "idle", "Arm powered, brakes released, no program playing", components, details=details)
        return Observation(True, "unknown", "unknown", f"Unrecognised robotmode {robotmode!r}", components, details=details)
