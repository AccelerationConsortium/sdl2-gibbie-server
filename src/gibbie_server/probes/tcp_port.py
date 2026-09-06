"""Reachability of a plain TCP endpoint, e.g. the Mettler Reactor Device
Server's OPC UA port or a bench Pi's SSH port.

The probe connects and closes. It sends no bytes and speaks no protocol, so it
cannot disturb whatever owns that socket. What the tile means is therefore
narrow and stated in the message: **something is listening there**. It does not
mean the instrument behind it is healthy, idle, or even attached.

This is deliberately the probe of last resort. When a device speaks a protocol
we can safely read (a UR dashboard, an HTTP endpoint), use that instead and say
something truer. Use this one when the only honest observation available is
that the port answers.
"""

from __future__ import annotations

import socket
import time
from typing import ClassVar

from sdl_lab_contract import ComponentStatus, MetricValue

from .base import Observation, Probe

_TIMEOUT = 2.0


class TcpPortProbe(Probe):
    probe_type: ClassVar[str] = "tcp_port"
    primary_operation: ClassVar[str] = (
        "not observed by this probe (a TCP connect proves a listener, not the instrument's operation)"
    )

    def __init__(self, device_id, cfg, *, connect=None) -> None:
        super().__init__(device_id, cfg)
        self.host = str(cfg.option("host", "127.0.0.1"))
        self.port = int(cfg.option("port"))
        # What is expected to be listening; used in the message so an operator
        # reading the tile knows what the open port actually implies.
        self.label = cfg.option("label") or None
        self.timeout = float(cfg.option("timeout_s", _TIMEOUT))
        self._connect = connect or socket.create_connection

    @property
    def target(self) -> str:
        return f"{self.host}:{self.port}"

    def probe(self) -> Observation:
        started = time.monotonic()
        try:
            sock = self._connect((self.host, self.port), timeout=self.timeout)
        except (OSError, socket.timeout) as exc:
            return Observation.unreachable(
                f"Nothing accepting connections at {self.target}"
                f"{f' ({self.label})' if self.label else ''}: {exc}"
            )
        try:
            sock.close()
        except OSError:  # closing our own side cannot change what we observed
            pass
        latency_ms = round((time.monotonic() - started) * 1000, 1)
        what = self.label or "Port"
        return Observation(
            reachable=True,
            state="ready",
            activity="idle",
            message=f"{what} accepting connections at {self.target}; operating state not monitored",
            components={"endpoint": ComponentStatus(connected=True, state="listening", message=self.label)},
            metrics={"connect_latency": MetricValue(value=latency_ms, unit="ms")},
            details={"host": self.host, "port": self.port, "listener": self.label},
        )
