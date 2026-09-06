from fastapi.testclient import TestClient
from sdl_lab_contract import EquipmentStatus

from gibbie_server.api import create_app
from gibbie_server.monitor import Monitor
from gibbie_server.probes.base import Observation

from test_monitor_envelope import FakeProbe
from conftest import config_for, device


def _client():
    cfg = config_for(dev=device("serial_port", kind="robot_arm"))
    probe = FakeProbe("dev", cfg.devices["dev"])
    mon = Monitor(cfg, probes={"dev": probe})
    app = create_app(cfg, monitor=mon, start_monitor=False)
    return TestClient(app), mon, probe


def test_probe_health_and_service_status_shapes():
    c, _, _ = _client()
    assert c.get("/").json()["protocol_version"] == "1.2"
    assert c.get("/health").json() == {"status": "healthy"}
    status = EquipmentStatus(**c.get("/status").json())
    assert status.equipment_id == "gibbie_server" and status.allowed_actions == []


def test_device_listing_and_per_device_status():
    c, mon, _ = _client()
    listing = c.get("/devices").json()["devices"]
    assert listing[0]["status_path"] == "/devices/dev/status"
    mon.poll_once()
    env = EquipmentStatus(**c.get("/devices/dev/status").json())
    assert env.equipment_kind == "robot_arm" and env.equipment_status == "ready"
    assert c.get("/devices/dev").json()["equipment_id"] == "dev"


def test_status_is_cache_only_and_unknown_device_is_404():
    c, _, probe = _client()
    calls = {"n": 0}
    real = probe.probe
    def counting():
        calls["n"] += 1
        return real()
    probe.probe = counting
    c.get("/devices/dev/status"); c.get("/status")
    assert calls["n"] == 0  # handlers never probe
    assert c.get("/devices/nope/status").status_code == 404
