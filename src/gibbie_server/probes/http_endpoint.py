"""Reachability of an HTTP endpoint, e.g. the Mettler XPR balance's SOAP service.

What this tile means: **the endpoint answers**. It does not observe the
device's operating state (a weighing in progress), so it describes the link
and says so. A balance-side state read can replace this later; until then the
tile is honest about what it saw.
"""

from __future__ import annotations

from typing import ClassVar

import httpx
from sdl_lab_contract import ComponentStatus

from .base import Observation, Probe

_TIMEOUT = 2.0


class HttpEndpointProbe(Probe):
    probe_type: ClassVar[str] = "http_endpoint"
    primary_operation: ClassVar[str] = (
        "not observed by this probe (the endpoint is a link, not the instrument's operation)"
    )

    def __init__(self, device_id, cfg, *, transport: httpx.BaseTransport | None = None) -> None:
        super().__init__(device_id, cfg)
        self.url = str(cfg.option("url"))
        self._transport = transport

    @property
    def target(self) -> str:
        return self.url

    def probe(self) -> Observation:
        try:
            with httpx.Client(timeout=_TIMEOUT, transport=self._transport) as c:
                resp = c.get(self.url)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            return Observation.unreachable(f"No answer from {self.url}: {exc}")
        # Any HTTP answer -- even 405/500 from a SOAP endpoint poked with GET --
        # proves the service is up. That is all this probe claims.
        return Observation(
            reachable=True, state="ready", activity="idle",
            message=f"Endpoint answering (HTTP {resp.status_code}); operating state not monitored",
            components={"endpoint": ComponentStatus(connected=True, state=f"http_{resp.status_code}")},
            details={"http_status": resp.status_code, "server": resp.headers.get("server")},
        )
