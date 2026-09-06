"""Monitor state -> STATUS_SPEC envelopes.

Read-only clause (§9): `allowed_actions` is `[]` and there is nothing to claim,
so the envelopes are v1.2 on the strength of the read side alone.
"""

from __future__ import annotations

import socket
from datetime import datetime, timezone
from typing import Any

from sdl_lab_contract import ComponentStatus, EquipmentStatus, MetricValue

from .monitor import DeviceRecord, Monitor
from .version import __version__

PROTOCOL_VERSION = "1.2"


def _host() -> str | None:
    try:
        return socket.gethostname()
    except OSError:
        return None


def device_envelope(rec: DeviceRecord, monitor: Monitor, *, now: datetime | None = None) -> EquipmentStatus:
    now = now or monitor.clock()
    with rec.lock:
        obs = rec.last
        details: dict[str, Any] = {
            "probe": rec.probe.probe_type,
            "probe_target": rec.probe.target,
            "primary_operation": rec.probe.primary_operation,
            "monitoring_only": True,
            "poll_interval_s": monitor.config.service.poll_interval_s,
            "last_probe_at": rec.last_probe_at,
            "last_seen_at": rec.last_seen_at,
            "readback_age_s": round((now - rec.last_seen_at).total_seconds(), 1) if rec.last_seen_at else None,
            "probe_failures": rec.probe_failures,
            "unreachable_since": rec.unreachable_since,
        }
        common = dict(
            protocol_version=PROTOCOL_VERSION,
            equipment_id=rec.device_id,
            equipment_name=rec.cfg.name,
            equipment_kind=rec.cfg.kind,
            equipment_version=__version__,
            host=_host(),
            device_time=now,
            uptime_seconds=(now - monitor.started_at).total_seconds(),
            allowed_actions=[],
            required_actions=[],
            activity=rec.activity,
            activity_since=rec.activity_since,
        )

        if rec.unreachable:
            since = rec.unreachable_since.isoformat(timespec="seconds")  # type: ignore[union-attr]
            seen = f"; last seen {rec.last_seen_at.isoformat(timespec='seconds')}" if rec.last_seen_at else "; never seen since this service started"
            # §2.1: the gateway is fine, the device cannot be reached -> unknown, never error.
            failure = obs.message if obs is not None else ""
            return EquipmentStatus(
                **common, equipment_status="unknown",
                message=f"{rec.cfg.name} unreachable ({rec.probe.target}) since {since}{seen}: {failure}",
                components={"link": ComponentStatus(connected=False, state="unreachable", message=failure, last_event_at=rec.last_seen_at)},
                details={**details, **(rec.last_good.details if rec.last_good else {})},
            )
        good = rec.last_good
        if good is None:
            # Never reached yet and not (yet) declared unreachable.
            note = f"Not probed yet" if obs is None else f"Not reached yet ({obs.message}); below unreachable_after threshold"
            return EquipmentStatus(**common, equipment_status="unknown", message=note, components={}, details=details)
        if obs is not None and not obs.reachable:
            # Failed probe(s) below the debounce threshold: the last good observation stands.
            details["note"] = f"last probe failed ({obs.message}); below unreachable_after threshold"
        obs = good
        return EquipmentStatus(
            **common,
            equipment_status=obs.state,
            message=obs.message,
            components=obs.components,
            metrics=obs.metrics,
            last_error=obs.last_error,
            details={**details, **obs.details},
        )


def service_envelope(monitor: Monitor, *, equipment_id: str = "gibbie_server", now: datetime | None = None) -> EquipmentStatus:
    """The monitor process itself. A monitor has no primary operation, so it is
    permanently idle (the same reading as a passive sensor gateway)."""

    now = now or monitor.clock()
    devices = {}
    for rec in monitor.records.values():
        with rec.lock:
            devices[rec.device_id] = {
                "state": "unknown" if rec.unreachable or rec.last is None else rec.last.state,
                "unreachable": rec.unreachable,
                "last_seen_at": rec.last_seen_at,
            }
    alive = monitor.alive
    return EquipmentStatus(
        protocol_version=PROTOCOL_VERSION,
        equipment_id=equipment_id,
        equipment_name="Gibbie Monitor",
        equipment_kind="other",
        equipment_version=__version__,
        host=_host(),
        equipment_status="ready" if alive else "error",
        message=f"Monitoring {len(devices)} devices every {monitor.config.service.poll_interval_s:g} s" if alive
                else "Monitor thread is not running",
        activity="idle",
        activity_since=monitor.started_at,
        device_time=now,
        uptime_seconds=(now - monitor.started_at).total_seconds(),
        components={"monitor": ComponentStatus(connected=alive, state="polling" if alive else "stopped")},
        metrics={"devices_unreachable": MetricValue(value=sum(1 for d in devices.values() if d["unreachable"]), unit="count")},
        allowed_actions=[],
        details={"monitoring_only": True, "devices": devices},
    )
