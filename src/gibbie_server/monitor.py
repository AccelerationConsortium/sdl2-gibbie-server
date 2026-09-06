"""Background poller with per-device outage debounce.

The monitor is the only thing that talks to hardware. `/status` reads the
cache it keeps. The debounce mirrors the OT-2 gateway's reachability monitor:
`unreachable_after` consecutive failed probes open an outage span; the first
success closes it. Every probe result, success or failure, is recorded.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .config import Config, DeviceConfig
from .probes import Observation, Probe, build_probe

logger = logging.getLogger(__name__)


@dataclass
class DeviceRecord:
    device_id: str
    cfg: DeviceConfig
    probe: Probe
    last: Observation | None = None
    #: Most recent observation from a probe that reached the device. Below the
    #: debounce threshold this is what the envelope shows.
    last_good: Observation | None = None
    last_probe_at: datetime | None = None
    last_seen_at: datetime | None = None
    probe_failures: int = 0
    unreachable_since: datetime | None = None
    # Activity span tracking (§2.3): value + the instant it last changed.
    activity: str = "unknown"
    activity_since: datetime | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def unreachable(self) -> bool:
        return self.unreachable_since is not None


class Monitor:
    def __init__(
        self,
        config: Config,
        *,
        probes: dict[str, Probe] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config = config
        self.clock = clock
        self.records: dict[str, DeviceRecord] = {}
        for device_id, cfg in config.devices.items():
            probe = (probes or {}).get(device_id) or build_probe(device_id, cfg)
            self.records[device_id] = DeviceRecord(device_id=device_id, cfg=cfg, probe=probe)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started_at = self.clock()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="gibbie-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        # First sweep immediately, then every poll_interval_s.
        self.poll_once()
        while not self._stop.wait(self.config.service.poll_interval_s):
            self.poll_once()

    # -- polling --------------------------------------------------------------

    def poll_once(self) -> None:
        for rec in self.records.values():
            try:
                obs = rec.probe.probe()
            except Exception as exc:  # a probe bug must not take the loop down
                logger.exception("probe %s raised", rec.device_id)
                obs = Observation.unreachable(f"probe raised {type(exc).__name__}: {exc}")
            self.record(rec, obs)

    def record(self, rec: DeviceRecord, obs: Observation) -> None:
        now = self.clock()
        threshold = self.config.service.unreachable_after
        with rec.lock:
            rec.last_probe_at = now
            rec.last = obs
            if obs.reachable:
                rec.probe_failures = 0
                rec.last_seen_at = now
                rec.last_good = obs
                if rec.unreachable_since is not None:
                    outage = (now - rec.unreachable_since).total_seconds()
                    rec.unreachable_since = None
                    logger.warning("%s reachable again after %.0f s", rec.device_id, outage)
                self._note_activity(rec, obs.activity, now)
                return
            rec.probe_failures += 1
            if rec.unreachable_since is None and rec.probe_failures >= threshold:
                rec.unreachable_since = now
                logger.warning(
                    "%s unreachable at %s (%d consecutive probes failed): %s",
                    rec.device_id, rec.probe.target, rec.probe_failures, obs.message,
                )
            if rec.unreachable_since is not None:
                self._note_activity(rec, "unknown", now)

    @staticmethod
    def _note_activity(rec: DeviceRecord, activity: str, now: datetime) -> None:
        if activity != rec.activity:
            rec.activity = activity
            rec.activity_since = now
