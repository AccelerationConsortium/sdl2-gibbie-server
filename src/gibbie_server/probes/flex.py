"""Opentrons Flex, as seen from outside a Gibbie REPL session.

Two facts are observable without touching the robot:

1. **robot-server** (`GET /health` on 31950). 200 means the run engine is up and
   `/runs` tells whether a run is active. A **502 from nginx** means the robot
   is alive but robot-server is stopped -- the normal state while the Gibbie
   driver owns the hardware through its SSH REPL (only one process may hold
   the Flex's hardware controller). A connection failure means the robot or
   the network is down.
2. **A live REPL** (optional SSH probe): the Gibbie driver's REPL is a bare
   ``python3`` process on the robot. Its presence is external control, the
   same way an active external run is for the OT-2 gateway -- reported as
   `busy` + `running`, without claiming to know whether an axis is moving.

The camera stream is a component, never a reachability signal.
"""

from __future__ import annotations

import os
from typing import Any, Callable, ClassVar

import httpx
from sdl_lab_contract import ComponentStatus

from .base import Observation, Probe

_TIMEOUT = 2.0
_HEADERS = {"Opentrons-Version": "*"}
_TERMINAL = {"succeeded", "failed", "stopped"}

# Counts bare `python3` REPLs (cmdline exactly "python3\0"); robot-server and
# friends have longer cmdlines and cannot match. No `#` -- the robot's sh.
_REPL_COUNT_CMD = (
    "n=0; for d in /proc/[0-9]*; do "
    '[ -r "$d/cmdline" ] || continue; '
    "cmd=$(tr '\\0' '|' < \"$d/cmdline\"); "
    '[ "$cmd" = "python3|" ] && n=$((n+1)); '
    "done; echo $n"
)

SshRunner = Callable[[str, str], str]  # (alias, command) -> stdout


def _run_active(runs: list[dict[str, Any]]) -> bool:
    for run in runs:
        if not run.get("current"):
            continue
        status = run.get("status")
        if status in _TERMINAL:
            return False
        return not (status == "idle" and not run.get("startedAt"))
    return False


def paramiko_ssh_runner(alias: str, command: str) -> str:
    """Run one command on the robot via the ssh_config alias. Password from
    GIBBIE_FLEX_SSH_PASSWORD, key passphrase from GIBBIE_FLEX_SSH_KEY_PASSPHRASE."""

    import paramiko

    cfg = paramiko.SSHConfig()
    cfg_path = os.path.expanduser("~/.ssh/config")
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            cfg.parse(f)
    h = cfg.lookup(alias)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict[str, Any] = {
        "hostname": h.get("hostname", alias),
        "port": int(h.get("port", 22)),
        "username": h.get("user", "root"),
        "timeout": _TIMEOUT,
        "banner_timeout": 5,
        "auth_timeout": 5,
    }
    if h.get("identityfile"):
        kwargs["key_filename"] = [os.path.expanduser(p) for p in h["identityfile"]]
        if os.environ.get("GIBBIE_FLEX_SSH_KEY_PASSPHRASE"):
            kwargs["passphrase"] = os.environ["GIBBIE_FLEX_SSH_KEY_PASSPHRASE"]
    if os.environ.get("GIBBIE_FLEX_SSH_PASSWORD"):
        kwargs["password"] = os.environ["GIBBIE_FLEX_SSH_PASSWORD"]
    client.connect(**kwargs)
    try:
        _, stdout, _ = client.exec_command(command, timeout=5)
        return stdout.read().decode("utf-8", errors="replace").strip()
    finally:
        client.close()


class FlexProbe(Probe):
    probe_type: ClassVar[str] = "flex"
    primary_operation: ClassVar[str] = (
        "a protocol run on robot-server, or an SSH REPL session holding the hardware "
        "(the Gibbie driver) -- external control, as for the OT-2 gateway"
    )

    def __init__(
        self, device_id, cfg, *,
        transport: httpx.BaseTransport | None = None,
        ssh_runner: SshRunner | None = None,
    ) -> None:
        super().__init__(device_id, cfg)
        self.robot_server_url = str(cfg.option("robot_server_url", "http://192.168.254.81:31950")).rstrip("/")
        self.ssh_alias = cfg.option("ssh_alias")
        self.ssh_enabled = bool(cfg.option("ssh_enabled", bool(self.ssh_alias)))
        self.camera_url = cfg.option("camera_url")
        self._transport = transport
        self._ssh = ssh_runner or paramiko_ssh_runner

    @property
    def target(self) -> str:
        return self.robot_server_url

    # -- pieces -------------------------------------------------------------

    def _robot_server(self) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
        """('up'|'stopped', health, runs) or raises httpx.TransportError."""
        with httpx.Client(base_url=self.robot_server_url, timeout=_TIMEOUT, headers=_HEADERS, transport=self._transport) as c:
            r = c.get("/health")
            if r.status_code in (502, 503, 504):
                return "stopped", None, []
            r.raise_for_status()
            health = r.json()
            runs_resp = c.get("/runs")
            runs = runs_resp.json().get("data", []) if runs_resp.status_code == 200 else []
            return "up", health, runs

    def _repl_count(self) -> int | None:
        if not (self.ssh_enabled and self.ssh_alias):
            return None
        try:
            out = self._ssh(str(self.ssh_alias), _REPL_COUNT_CMD)
            return int(out.strip().splitlines()[-1])
        except Exception:
            return None

    def _camera(self) -> ComponentStatus:
        if not self.camera_url:
            return ComponentStatus(connected=False, state="not_configured")
        try:
            with httpx.Client(timeout=1.5, transport=self._transport) as c:
                with c.stream("GET", str(self.camera_url)) as r:
                    return ComponentStatus(connected=r.status_code == 200, state="streaming" if r.status_code == 200 else f"http_{r.status_code}")
        except (httpx.TransportError, httpx.TimeoutException):
            return ComponentStatus(connected=False, state="off")

    # -- observation --------------------------------------------------------

    def probe(self) -> Observation:
        try:
            server, health, runs = self._robot_server()
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            return Observation.unreachable(f"Robot not answering at {self.robot_server_url}: {exc}")
        except (httpx.HTTPStatusError, ValueError) as exc:
            return Observation(True, "error", "unknown", f"robot-server answered but broken: {exc}",
                               {"robot_server": ComponentStatus(connected=True, state="http_error")})
        repl = self._repl_count()
        camera = self._camera()
        return self.interpret(server, health, runs, repl, camera)

    def interpret(self, server: str, health: dict[str, Any] | None, runs: list[dict[str, Any]],
                  repl_count: int | None, camera: ComponentStatus) -> Observation:
        components = {
            "robot_server": ComponentStatus(connected=server == "up", state=server),
            "repl_session": ComponentStatus(
                connected=bool(repl_count),
                state="unknown" if repl_count is None else ("live" if repl_count else "none"),
                message=None if repl_count is None else f"{repl_count} bare python3 process(es)",
            ),
            "camera": camera,
        }
        details: dict[str, Any] = {"robot_server": server, "repl_sessions": repl_count}
        if health:
            details.update({k: health.get(k) for k in ("name", "robot_model", "api_version", "system_version") if k in health})

        if server == "up":
            if _run_active(runs):
                return Observation(True, "busy", "running", "robot-server run in progress (external control)", components, details=details)
            return Observation(True, "ready", "idle", "robot-server up, no run active", components, details=details)
        # robot-server stopped: the robot is alive (nginx answered).
        if repl_count:
            return Observation(True, "busy", "running",
                               "Under SSH REPL control (Gibbie driver); robot-server stopped", components, details=details)
        if repl_count is None:
            # Robot alive, robot-server stopped, and we cannot see whether a REPL
            # holds it: the state is genuinely undetermined (§2.1). Enable the SSH
            # probe to make this tile informative.
            return Observation(True, "unknown", "unknown",
                               "robot-server stopped; REPL presence not observable (SSH probe off or failed)", components, details=details)
        return Observation(True, "requires_init", "idle",
                           "robot-server stopped and no REPL session; start robot-server or a Gibbie session", components, details=details)
