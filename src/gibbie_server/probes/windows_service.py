"""State of a Windows service, e.g. the Agilent ChemStation Data Service.

Some instruments expose nothing on the network at all — the Process Chemistry
HPLC answers on none of its ports, because it is driven by ChemStation on the
bench PC rather than by an instrument-side API. For those, the honest
observable is the vendor service on that PC, which the service control manager
will report without anyone touching the instrument.

Read-only by construction: this runs `sc query`, never `sc start` / `stop`.
Restarting a service is a host-ops action with its own whitelist and audit
trail, and it does not belong in a monitor.

What the tile means: **the vendor's software layer is up**. It does not mean a
method is running, or that the instrument is even powered — the message says so.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Callable, ClassVar

from sdl_lab_contract import ComponentStatus, ErrorInfo

from .base import Observation, Probe

_TIMEOUT = 5.0
_STATE_RE = re.compile(r"STATE\s*:\s*\d+\s+([A-Z_]+)")
#: `sc` exit code for "the specified service does not exist".
_ERROR_SERVICE_DOES_NOT_EXIST = 1060

_RUNNING = "RUNNING"
_STOPPED = "STOPPED"
_PAUSED = "PAUSED"
_TRANSITIONAL = {"START_PENDING", "STOP_PENDING", "PAUSE_PENDING", "CONTINUE_PENDING"}


def _sc_path() -> str:
    # A Windows service does not inherit the interactive user's PATH, so resolve
    # explicitly and fall back to the known System32 location.
    return shutil.which("sc") or r"C:\Windows\System32\sc.exe"


def run_sc_query(service: str) -> tuple[int, str]:
    proc = subprocess.run(
        [_sc_path(), "query", service],
        capture_output=True, text=True, timeout=_TIMEOUT,
    )
    return proc.returncode, f"{proc.stdout}\n{proc.stderr}"


class WindowsServiceProbe(Probe):
    probe_type: ClassVar[str] = "windows_service"
    primary_operation: ClassVar[str] = (
        "not observed by this probe (the vendor service being up is not the instrument running a method)"
    )

    def __init__(self, device_id, cfg, *, query: Callable[[str], tuple[int, str]] | None = None) -> None:
        super().__init__(device_id, cfg)
        self.service = str(cfg.option("service"))
        # What this service means for the device, for the tile's message.
        self.label = cfg.option("label") or None
        self._query = query or run_sc_query

    @property
    def target(self) -> str:
        return f"service:{self.service}"

    def probe(self) -> Observation:
        try:
            code, output = self._query(self.service)
        except FileNotFoundError:
            return Observation.unreachable("`sc` not available on this host (not Windows?)")
        except subprocess.TimeoutExpired:
            return Observation.unreachable(f"`sc query {self.service}` timed out")
        except OSError as exc:
            return Observation.unreachable(f"Could not query the service control manager: {exc}")

        now = datetime.now(timezone.utc)
        if code == _ERROR_SERVICE_DOES_NOT_EXIST:
            # The SCM answered; what it says is that the configured service is
            # not installed. That is an observed fault, not a reachability gap.
            return Observation(
                reachable=True, state="error", activity="unknown",
                message=f"Service {self.service!r} is not installed on this PC",
                components={"service": ComponentStatus(connected=False, state="not_installed")},
                details={"service": self.service, "sc_exit_code": code},
                last_error=ErrorInfo(code="service_not_installed", message=self.service,
                                     severity="error", timestamp=now),
            )

        match = _STATE_RE.search(output)
        if match is None:
            return Observation.unreachable(
                f"Could not read a STATE line from `sc query {self.service}` (exit {code})"
            )
        state = match.group(1)
        what = self.label or f"{self.service}"
        details = {"service": self.service, "service_state": state, "meaning": self.label}
        component = {"service": ComponentStatus(connected=state == _RUNNING, state=state.lower(), message=self.label)}

        if state == _RUNNING:
            return Observation(
                reachable=True, state="ready", activity="idle",
                message=f"{what} running; the instrument's own state is not monitored",
                components=component, details=details,
            )
        if state == _STOPPED:
            # Not `error`: nothing faulted, the software layer simply is not up.
            # The instrument may be perfectly fine and driven from its own front
            # end, which is exactly why this must not read as a fault.
            return Observation(
                reachable=True, state="requires_init", activity="idle",
                message=f"{what} is stopped; start it on the bench PC to restore the data path",
                components=component, details=details,
            )
        if state == _PAUSED:
            return Observation(
                reachable=True, state="degraded", activity="idle",
                message=f"{what} is paused", components=component, details=details,
            )
        if state in _TRANSITIONAL:
            return Observation(
                reachable=True, state="unknown", activity="unknown",
                message=f"{what} is {state.replace('_', ' ').lower()}",
                components=component, details=details,
            )
        return Observation(
            reachable=True, state="unknown", activity="unknown",
            message=f"{what} reports an unrecognised state {state!r}",
            components=component, details=details,
        )
