import json

import httpx
from sdl_lab_contract import ComponentStatus

from gibbie_server.probes.flex import FlexProbe
from gibbie_server.probes.http_endpoint import HttpEndpointProbe
from gibbie_server.probes.serial_port import SerialPortProbe
from gibbie_server.probes.tcp_port import TcpPortProbe
from gibbie_server.probes.ui_bridge import UiBridgeProbe
from gibbie_server.probes.ur_dashboard import UrDashboardProbe
from gibbie_server.probes.windows_service import WindowsServiceProbe

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


# -- TCP port ----------------------------------------------------------------

class _Sock:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_tcp_port_listening_is_reachable_and_says_what_that_means():
    sock = _Sock()
    seen: dict = {}

    def connect(address, timeout=None):
        seen["address"], seen["timeout"] = address, timeout
        return sock

    p = TcpPortProbe("e", device("tcp_port", host="127.0.0.1", port=50008,
                                 label="Reactor Device Server (OPC UA)"), connect=connect)
    obs = p.probe()
    assert obs.reachable and obs.state == "ready" and obs.activity == "idle"
    assert seen["address"] == ("127.0.0.1", 50008)
    assert sock.closed, "the probe must not hold the socket open"
    assert "Reactor Device Server (OPC UA)" in obs.message
    assert "operating state not monitored" in obs.message
    assert obs.components["endpoint"].state == "listening"
    assert obs.metrics["connect_latency"].unit == "ms"


def test_tcp_port_refused_is_unreachable_not_error():
    def refuse(address, timeout=None):
        raise ConnectionRefusedError("connection refused")

    obs = TcpPortProbe("e", device("tcp_port", port=50008, label="OPC UA"), connect=refuse).probe()
    assert not obs.reachable and obs.state == "unknown"
    assert "OPC UA" in obs.message


def test_tcp_port_defaults_to_loopback_and_honours_a_custom_timeout():
    seen: dict = {}

    def connect(address, timeout=None):
        seen["address"], seen["timeout"] = address, timeout
        return _Sock()

    TcpPortProbe("e", device("tcp_port", port=22, timeout_s=0.5), connect=connect).probe()
    assert seen["address"] == ("127.0.0.1", 22) and seen["timeout"] == 0.5


# -- Windows service ---------------------------------------------------------

def _sc(state: str, code: int = 0):
    output = f"SERVICE_NAME: svc\n        TYPE               : 10  WIN32_OWN_PROCESS\n        STATE              : 4  {state}\n"
    return lambda service: (code, output)


def test_windows_service_running_is_ready_and_disclaims_the_instrument():
    obs = WindowsServiceProbe("h", device("windows_service", kind="hplc", service="Agilent Chemstation Data Service",
                                          label="Agilent ChemStation data service"), query=_sc("RUNNING")).probe()
    assert obs.reachable and obs.state == "ready" and obs.activity == "idle"
    assert "instrument's own state is not monitored" in obs.message
    assert obs.components["service"].connected and obs.details["service_state"] == "RUNNING"


def test_windows_service_stopped_is_requires_init_never_error():
    # The instrument may be fine and driven from its own front end, so a stopped
    # vendor service must not read as a hardware fault.
    obs = WindowsServiceProbe("h", device("windows_service", service="svc"), query=_sc("STOPPED")).probe()
    assert obs.reachable and obs.state == "requires_init" and obs.last_error is None
    assert "stopped" in obs.message


def test_windows_service_paused_and_transitional_states():
    paused = WindowsServiceProbe("h", device("windows_service", service="svc"), query=_sc("PAUSED")).probe()
    starting = WindowsServiceProbe("h", device("windows_service", service="svc"), query=_sc("START_PENDING")).probe()
    assert paused.state == "degraded"
    assert starting.state == "unknown" and "start pending" in starting.message


def test_windows_service_not_installed_is_a_reported_fault_not_a_link_failure():
    obs = WindowsServiceProbe("h", device("windows_service", service="Nope"),
                              query=lambda s: (1060, "The specified service does not exist")).probe()
    assert obs.reachable and obs.state == "error"
    assert obs.last_error is not None and obs.last_error.code == "service_not_installed"


def test_windows_service_unparseable_or_missing_sc_is_unreachable():
    garbled = WindowsServiceProbe("h", device("windows_service", service="svc"),
                                  query=lambda s: (0, "not a service listing")).probe()
    assert not garbled.reachable

    def no_sc(service):
        raise FileNotFoundError("sc")

    missing = WindowsServiceProbe("h", device("windows_service", service="svc"), query=no_sc).probe()
    assert not missing.reachable and "not Windows" in missing.message


def test_windows_service_never_shells_out_to_a_mutating_verb():
    import inspect

    from gibbie_server.probes import windows_service

    source = inspect.getsource(windows_service)
    for verb in ('"start"', '"stop"', '"pause"', '"continue"', '"config"'):
        assert verb not in source, f"a monitor must never run sc {verb}"


# -- UR CB3 fallback ---------------------------------------------------------

class _CB3Dashboard:
    """A PolyScope 3.x dashboard: knows `safetymode`, rejects `safetystatus`."""

    def __init__(self, host, port, timeout=None) -> None:
        self.asked: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def query(self, command: str) -> str:
        self.asked.append(command)
        return {
            "robotmode": "Robotmode: RUNNING",
            "safetystatus": "Unknown command",
            "safetymode": "Safetymode: NORMAL",
            "programState": "STOPPED prog.urp",
            "get loaded program": "Loaded program: /programs/prog.urp",
        }[command]


def test_ur_dashboard_falls_back_to_safetymode_on_a_cb3():
    seen = {}

    def factory(host, port, timeout=None):
        seen["dash"] = _CB3Dashboard(host, port)
        return seen["dash"]

    obs = UrDashboardProbe("a", device("ur_dashboard", kind="robot_arm", host="192.168.254.16"),
                           client_factory=factory).probe()
    assert obs.reachable and obs.state == "ready"
    assert seen["dash"].asked[:3] == ["robotmode", "safetystatus", "safetymode"]
    assert obs.details["safety_source"] == "safetymode"
    assert obs.details["safetystatus"] == "NORMAL"
