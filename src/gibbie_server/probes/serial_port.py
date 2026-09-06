"""Presence of a serial port, e.g. the IKA hotplate on COM7.

The workflow owns the port and a serial port cannot be shared, so this probe
NEVER opens it. What the tile means: **the port is enumerated**. The device's
operating state (heating, stirring) is not observed and the message says so.
"""

from __future__ import annotations

from typing import Any, Callable, ClassVar

from sdl_lab_contract import ComponentStatus

from .base import Observation, Probe


def list_ports() -> list[Any]:
    from serial.tools import list_ports as lp

    return list(lp.comports())


class SerialPortProbe(Probe):
    probe_type: ClassVar[str] = "serial_port"
    primary_operation: ClassVar[str] = (
        "not observed by this probe (the port is owned by the workflow and never opened here)"
    )

    def __init__(self, device_id, cfg, *, ports: Callable[[], list[Any]] | None = None) -> None:
        super().__init__(device_id, cfg)
        self.port = str(cfg.option("port", "COM7")).upper()
        self._ports = ports or list_ports

    @property
    def target(self) -> str:
        return self.port

    def probe(self) -> Observation:
        try:
            ports = self._ports()
        except Exception as exc:  # enumeration itself failed: nothing observed
            return Observation.unreachable(f"Serial enumeration failed: {exc}")
        match = next((p for p in ports if str(getattr(p, "device", p)).upper() == self.port), None)
        if match is None:
            return Observation.unreachable(
                f"{self.port} not enumerated (adapter unplugged or device off)",
                ports_present=[str(getattr(p, "device", p)) for p in ports],
            )
        desc = getattr(match, "description", None)
        return Observation(
            reachable=True, state="ready", activity="idle",
            message=f"{self.port} present ({desc or 'serial device'}); operating state not monitored",
            components={"serial_port": ComponentStatus(connected=True, state="enumerated", message=desc)},
            details={"port": self.port, "description": desc, "hwid": getattr(match, "hwid", None)},
        )
