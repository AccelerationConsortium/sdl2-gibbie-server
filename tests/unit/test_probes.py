import json

import httpx
from sdl_lab_contract import ComponentStatus

from gibbie_server.probes.flex import FlexProbe
from gibbie_server.probes.http_endpoint import HttpEndpointProbe
from gibbie_server.probes.serial_port import SerialPortProbe
from gibbie_server.probes.ui_bridge import UiBridgeProbe
from gibbie_server.probes.ur_dashboard import UrDashboardProbe

from conftest import device


# -- UI bridge ---------------------------------------------------------------

def _bridge_transport(health: dict, status: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/health":
            return httpx.Response(200, json=health)
        if request.url.path == "/api/status":
            return httpx.Response(200, json=status)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_ui_bridge_running_run_is_busy_with_the_live_stage():
    p = UiBridgeProbe("w", device("ui_bridge"), transport=_bridge_transport(
        {"ok": True, "backend_loaded": True, "running": True},
        {"ok": True, "run_state": {"running": True, "simulation": False,
                                   "stages": {"dose": "done", "sample": "running", "hplc": "pending"}}, "last_warnings": []},
    ))
    obs = p.probe()
    assert obs.reachable and obs.state == "busy" and obs.activity == "running"
    assert "sample" in obs.message and "1/3" in obs.message


def test_ui_bridge_simulated_run_is_dry_run_not_lab_work():
    p = UiBridgeProbe("w", device("ui_bridge"), transport=_bridge_transport(
        {"ok": True, "backend_loaded": True, "running": True},
        {"ok": True, "run_state": {"running": True, "simulation": True, "stages": {}}},
    ))
    obs = p.probe()
    assert obs.state == "dry_run" and obs.activity == "running"
    assert obs.message.startswith("[SIMULATION]")


def test_ui_bridge_idle_and_failed_run_and_broken_backend():
    idle = UiBridgeProbe("w", device("ui_bridge"), transport=_bridge_transport(
        {"ok": True, "backend_loaded": True, "running": False}, {"ok": True, "run_state": {"running": False}})).probe()
    assert idle.state == "ready" and idle.activity == "idle"

    failed = UiBridgeProbe("w", device("ui_bridge"), transport=_bridge_transport(
        {"ok": True, "backend_loaded": True, "running": False},
        {"ok": True, "run_state": {"running": False, "error": "RuntimeError: tip", "finished_at": "2026-09-06T01:02:03"}})).probe()
    assert failed.state == "error" and failed.last_error.code == "workflow_failed"

    broken = UiBridgeProbe("w", device("ui_bridge"), transport=_bridge_transport(
        {"ok": True, "backend_loaded": False, "backend_error": "ImportError: rtde", "running": False},
        {"ok": True, "run_state": {"running": False}})).probe()
    assert broken.state == "degraded" and broken.components["workflow_backend"].connected is False


def test_ui_bridge_down_is_unreachable_not_error():
    def handler(request):
        raise httpx.ConnectError("refused")
    obs = UiBridgeProbe("w", device("ui_bridge"), transport=httpx.MockTransport(handler)).probe()
    assert obs.reachable is False and obs.state == "unknown"


# -- UR dashboard ------------------------------------------------------------

def test_ur_dashboard_mapping():
    I = UrDashboardProbe.interpret
    playing = I("RUNNING", "NORMAL", "PLAYING rtde_control.urp", "Loaded program: /programs/rtde_control.urp")
    assert playing.state == "busy" and playing.activity == "running"
    assert playing.components["program"].message == "rtde_control.urp"

    idle = I("RUNNING", "NORMAL", "STOPPED rtde_control.urp", "Loaded program: x")
    assert idle.state == "ready" and idle.activity == "idle"

    off = I("POWER_OFF", "NORMAL", "STOPPED", "No program loaded")
    assert off.state == "requires_init" and off.activity == "idle"

    pstop = I("RUNNING", "PROTECTIVE_STOP", "PAUSED x", "Loaded program: x")
    assert pstop.state == "error" and pstop.last_error.code == "ur_safety_stop"

    estop = I("IDLE", "ROBOT_EMERGENCY_STOP", "STOPPED", "Loaded program: x")
    assert estop.state == "e_stop" and estop.activity == "idle"

    lost = I("NO_CONTROLLER", "NORMAL", "", "")
    assert lost.state == "error" and lost.components["controller"].connected is False


def test_ur_dashboard_socket_failure_is_unreachable():
    class Boom:
        def __init__(self, host, port, timeout=2.0): pass
        def __enter__(self): raise OSError("no route to host")
        def __exit__(self, *a): pass
    obs = UrDashboardProbe("ur", device("ur_dashboard", kind="robot_arm"), client_factory=Boom).probe()
    assert obs.reachable is False and "no route" in obs.message


# -- HTTP endpoint -----------------------------------------------------------

def test_http_endpoint_any_answer_is_reachable():
    t = httpx.MockTransport(lambda r: httpx.Response(405, headers={"server": "gSOAP"}))
    obs = HttpEndpointProbe("b", device("http_endpoint", url="http://192.168.1.1:8002/MT"), transport=t).probe()
    assert obs.reachable and obs.state == "ready" and obs.details["http_status"] == 405
    assert "not monitored" in obs.message


# -- Flex --------------------------------------------------------------------

def _flex_transport(health_status=200, runs=None, camera=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port == 8080:
            return httpx.Response(camera, content=b"--frame")
        if request.url.path == "/health":
            return httpx.Response(health_status, json={"name": "8ccec7", "robot_model": "OT-3 Standard", "api_version": "8.5.0"} if health_status == 200 else None,
                                  content=None if health_status == 200 else b"<html>502</html>")
        if request.url.path == "/runs":
            return httpx.Response(200, json={"data": runs or []})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def _flex(transport, repl_out="0", ssh=True):
    cfg = device("flex", kind="liquid_handler", robot_server_url="http://flex:31950", ssh_alias="otflex",
                 ssh_enabled=ssh, camera_url="http://flex:8080/video")
    return FlexProbe("f", cfg, transport=transport, ssh_runner=lambda alias, cmd: repl_out)


def test_flex_robot_server_up_idle_and_running():
    idle = _flex(_flex_transport()).probe()
    assert idle.state == "ready" and idle.activity == "idle" and idle.details["api_version"] == "8.5.0"
    run = _flex(_flex_transport(runs=[{"current": True, "status": "running", "startedAt": "t"}])).probe()
    assert run.state == "busy" and run.activity == "running"
    own_open_run = _flex(_flex_transport(runs=[{"current": True, "status": "idle", "startedAt": None}])).probe()
    assert own_open_run.state == "ready"  # an unstarted idle run is a container, not work


def test_flex_502_means_robot_server_stopped_and_repl_decides():
    repl = _flex(_flex_transport(health_status=502), repl_out="1").probe()
    assert repl.state == "busy" and repl.activity == "running"
    assert repl.components["robot_server"].state == "stopped" and repl.components["repl_session"].connected

    none = _flex(_flex_transport(health_status=502), repl_out="0").probe()
    assert none.state == "requires_init" and none.activity == "idle"

    no_ssh = _flex(_flex_transport(health_status=502), ssh=False).probe()
    assert no_ssh.state == "unknown" and no_ssh.activity == "unknown"
    assert no_ssh.components["repl_session"].state == "unknown"


def test_flex_camera_is_a_component_never_a_reachability_signal():
    obs = _flex(_flex_transport(camera=503)).probe()
    assert obs.reachable and obs.components["camera"].connected is False


def test_flex_connection_failure_is_unreachable():
    def handler(request):
        raise httpx.ConnectTimeout("timeout")
    obs = _flex(httpx.MockTransport(handler)).probe()
    assert obs.reachable is False


# -- serial port -------------------------------------------------------------

class _Port:
    def __init__(self, device, description="USB Serial", hwid="USB VID:PID=0403:6001"):
        self.device, self.description, self.hwid = device, description, hwid


def test_serial_port_presence_only():
    present = SerialPortProbe("h", device("serial_port", port="COM7"), ports=lambda: [_Port("COM3"), _Port("COM7")]).probe()
    assert present.reachable and present.state == "ready" and "not monitored" in present.message
    absent = SerialPortProbe("h", device("serial_port", port="COM7"), ports=lambda: [_Port("COM3")]).probe()
    assert absent.reachable is False and absent.details["ports_present"] == ["COM3"]
