"""Probe registry. Each module observes one device type, read-only."""

from __future__ import annotations

from ..config import DeviceConfig
from .base import Observation, Probe
from .flex import FlexProbe
from .http_endpoint import HttpEndpointProbe
from .serial_port import SerialPortProbe
from .tcp_port import TcpPortProbe
from .ui_bridge import UiBridgeProbe
from .ur_dashboard import UrDashboardProbe
from .windows_service import WindowsServiceProbe

PROBES: dict[str, type[Probe]] = {
    UiBridgeProbe.probe_type: UiBridgeProbe,
    UrDashboardProbe.probe_type: UrDashboardProbe,
    HttpEndpointProbe.probe_type: HttpEndpointProbe,
    FlexProbe.probe_type: FlexProbe,
    SerialPortProbe.probe_type: SerialPortProbe,
    TcpPortProbe.probe_type: TcpPortProbe,
    WindowsServiceProbe.probe_type: WindowsServiceProbe,
}


def build_probe(device_id: str, cfg: DeviceConfig) -> Probe:
    try:
        cls = PROBES[cfg.probe]
    except KeyError:
        raise ValueError(
            f"[devices.{device_id}] probe {cfg.probe!r} unknown; one of {sorted(PROBES)}"
        ) from None
    return cls(device_id, cfg)


__all__ = ["PROBES", "Observation", "Probe", "build_probe"]
