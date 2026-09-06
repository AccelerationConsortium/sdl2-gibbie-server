"""Debounce + envelope: the same discipline as the OT-2 gateway's monitor."""

from datetime import datetime, timedelta, timezone

from sdl_lab_contract import ComponentStatus, EquipmentStatus

from gibbie_server.envelope import device_envelope, service_envelope
from gibbie_server.monitor import Monitor
from gibbie_server.probes.base import Observation, Probe

from conftest import config_for, device


class FakeProbe(Probe):
    probe_type = "fake"
    primary_operation = "pretending"

    def __init__(self, device_id, cfg):
        super().__init__(device_id, cfg)
        self.next = Observation(True, "ready", "idle", "fine", {"x": ComponentStatus(connected=True, state="ok")})

    @property
    def target(self):
        return "fake://dev"

    def probe(self):
        return self.next


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def tick(self, s=10):
        self.now += timedelta(seconds=s)


def _monitor():
    cfg = config_for(dev=device("serial_port"))  # probe type irrelevant: injected below
    clock = Clock()
    probe = FakeProbe("dev", cfg.devices["dev"])
    mon = Monitor(cfg, probes={"dev": probe}, clock=clock)
    return mon, probe, clock


def test_never_probed_reads_unknown_not_ready():
    mon, _, _ = _monitor()
    env = device_envelope(mon.records["dev"], mon)
    assert env.equipment_status == "unknown"
    assert env.activity == "unknown"
    assert env.allowed_actions == []
    EquipmentStatus.model_validate(env.model_dump(mode="json"))


def test_healthy_observation_passes_through_with_age():
    mon, _, clock = _monitor()
    mon.poll_once()
    clock.tick(7)
    env = device_envelope(mon.records["dev"], mon)
    assert env.equipment_status == "ready" and env.activity == "idle"
    assert env.details["readback_age_s"] == 7.0
    assert env.details["monitoring_only"] is True
    assert env.components["x"].connected is True


def test_one_failure_is_not_an_outage_but_two_are():
    mon, probe, clock = _monitor()
    mon.poll_once()
    probe.next = Observation.unreachable("nope")
    mon.poll_once()
    env = device_envelope(mon.records["dev"], mon)
    assert env.equipment_status == "ready"          # last good observation stands
    assert env.details["probe_failures"] == 1
    assert "below unreachable_after" in env.details["note"]

    clock.tick()
    mon.poll_once()
    env = device_envelope(mon.records["dev"], mon)
    assert env.equipment_status == "unknown"        # §2.1, never error
    assert env.activity == "unknown"
    assert env.last_error is None
    assert "unreachable" in env.message and "fake://dev" in env.message
    assert env.components["link"].state == "unreachable"
    assert env.details["unreachable_since"] is not None


def test_recovery_closes_the_span_and_restamps_activity():
    mon, probe, clock = _monitor()
    mon.poll_once()
    probe.next = Observation.unreachable("nope")
    mon.poll_once(); clock.tick(); mon.poll_once()
    assert mon.records["dev"].unreachable

    clock.tick()
    probe.next = Observation(True, "busy", "running", "working")
    mon.poll_once()
    env = device_envelope(mon.records["dev"], mon)
    assert env.equipment_status == "busy" and env.activity == "running"
    assert env.activity_since == clock.now
    assert env.details["unreachable_since"] is None


def test_probe_exception_counts_as_a_failed_probe_not_a_crash():
    mon, probe, _ = _monitor()

    def boom():
        raise RuntimeError("driver exploded")

    probe.probe = boom
    mon.poll_once()
    rec = mon.records["dev"]
    assert rec.probe_failures == 1 and rec.last.reachable is False
    assert "driver exploded" in rec.last.message


def test_service_envelope_is_permanently_idle_and_counts_outages():
    mon, probe, clock = _monitor()
    probe.next = Observation.unreachable("nope")
    mon.poll_once(); clock.tick(); mon.poll_once()
    env = service_envelope(mon)
    assert env.equipment_kind == "other" and env.activity == "idle"
    assert env.metrics["devices_unreachable"].value == 1
    assert env.details["devices"]["dev"]["unreachable"] is True
    # Thread not started in tests -> the service says so instead of claiming ready.
    assert env.equipment_status == "error"
