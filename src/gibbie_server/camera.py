"""Demand-driven Opentrons Flex deck-camera relay.

The public surface is one HTTP/MJPEG stream.  Its upstream follows the Flex
control mode: robot-server's official snapshot endpoint while HTTP is up, and
the same V4L2-over-SSH channel used by the Gibbie workflow while a REPL owns
the robot.  Capture starts only while at least one downstream viewer exists.
"""

from __future__ import annotations

import logging
import shlex
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
from sdl_lab_contract import ComponentStatus

from .config import DeviceConfig
from .probes.flex import open_ssh_client

logger = logging.getLogger(__name__)

_HEADERS = {"Opentrons-Version": "3"}
# The deck camera's stable udev link. `/dev/ot_system_camera` does not exist on
# the Gibbie Flex (robot-server 8.6.0, checked 2026-11-15); the camera is the
# capture node of the USB "HD USB Camera" (`index0`; `index1` is its metadata
# node), which by-id keeps stable across reboots and re-plugs.
DEFAULT_CAMERA_DEVICE = "/dev/v4l/by-id/usb-HD_USB_Camera_HD_USB_Camera-video-index0"
_BOUNDARY = b"frame"


def _jpeg(data: bytes) -> bytes:
    """Return the first complete JPEG in *data*, rejecting non-image replies."""
    start = data.find(b"\xff\xd8")
    end = data.find(b"\xff\xd9", start + 2)
    if start < 0 or end < 0:
        raise ValueError("camera response did not contain a complete JPEG")
    return data[start : end + 2]


class FlexCamera:
    """Share one upstream capture among any number of MJPEG viewers."""

    def __init__(
        self,
        cfg: DeviceConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        ssh_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.robot_server_url = str(cfg.option("robot_server_url")).rstrip("/")
        self.device = str(cfg.option("camera_device", DEFAULT_CAMERA_DEVICE))
        self.width = int(cfg.option("camera_width", 1280))
        self.height = int(cfg.option("camera_height", 720))
        self.http_interval = float(cfg.option("camera_http_interval_s", 0.2))
        if self.width <= 0 or self.height <= 0 or self.http_interval <= 0:
            raise ValueError("camera dimensions and camera_http_interval_s must be positive")
        self._transport = transport
        self._ssh_factory = ssh_factory or (lambda: open_ssh_client(
            str(cfg.option("ssh_alias", "otflex")),
            host=cfg.option("ssh_host"),
            user=cfg.option("ssh_user"),
            port=cfg.option("ssh_port"),
            key_file=cfg.option("ssh_key_file"),
        ))

        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._subscribers = 0
        self._sequence = 0
        self._latest: bytes | None = None
        self._mode: str | None = None
        self._error: str | None = None
        self._active_channel: Any | None = None
        self._closed = False

    def component_status(self) -> ComponentStatus:
        with self._condition:
            if not self._subscribers:
                return ComponentStatus(connected=False, state="off")
            if self._latest is not None:
                return ComponentStatus(
                    connected=True,
                    state="streaming",
                    message=f"deck camera streaming via {self._mode}",
                )
            return ComponentStatus(
                connected=False,
                state="starting" if not self._error else "unavailable",
                message=self._error or f"starting deck camera via {self._mode or 'auto'}",
            )

    def close(self) -> None:
        self._closed = True
        self._stop.set()
        with self._condition:
            channel = self._active_channel
            self._condition.notify_all()
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)

    def multipart(self) -> Iterator[bytes]:
        """Yield a shared multipart stream; first/last consumer owns capture."""
        with self._condition:
            if self._closed:
                raise RuntimeError("camera relay is closed")
            self._subscribers += 1
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._latest = None
                self._error = None
                self._thread = threading.Thread(
                    target=self._produce, name="flex-deck-camera", daemon=True
                )
                self._thread.start()
            else:
                # A viewer can reopen while the previous generator is still
                # unwinding. Keep that producer alive; its finalizer below also
                # covers the smaller race where it has already decided to exit.
                self._stop.clear()
            last = self._sequence
        try:
            while not self._stop.is_set():
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._sequence != last or self._stop.is_set(), timeout=5
                    )
                    if self._stop.is_set():
                        return
                    if self._sequence == last or self._latest is None:
                        continue
                    frame = self._latest
                    last = self._sequence
                yield (
                    b"--" + _BOUNDARY + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
                    + frame
                    + b"\r\n"
                )
        finally:
            with self._condition:
                self._subscribers -= 1
                if self._subscribers == 0:
                    self._stop.set()
                    channel = self._active_channel
                    self._condition.notify_all()
                else:
                    channel = None
            if channel is not None:
                try:
                    channel.close()
                except Exception:
                    pass

    def _publish(self, frame: bytes, mode: str) -> None:
        with self._condition:
            self._latest = frame
            self._mode = mode
            self._error = None
            self._sequence += 1
            self._condition.notify_all()

    def _robot_server_up(self) -> bool:
        with httpx.Client(
            base_url=self.robot_server_url,
            headers=_HEADERS,
            timeout=2,
            transport=self._transport,
        ) as client:
            response = client.get("/health")
            if response.status_code == 200:
                return True
            if response.status_code in (502, 503, 504):
                return False
            response.raise_for_status()
            return False

    def _produce(self) -> None:
        while not self._stop.is_set():
            try:
                if self._robot_server_up():
                    self._mode = "http"
                    self._produce_http()
                else:
                    self._mode = "ssh"
                    self._produce_ssh()
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                if self._stop.is_set():
                    # The last viewer left and closed the channel under us:
                    # an ordinary end of capture, not a transport failure.
                    logger.debug("Flex camera %s capture ended: %s", self._mode, message)
                    continue
                logger.warning("Flex camera %s transport failed: %s", self._mode, message)
                with self._condition:
                    self._latest = None
                    self._error = message
                    self._condition.notify_all()
            if not self._stop.wait(1):
                continue
        with self._condition:
            self._latest = None
            self._mode = None
            self._active_channel = None
            self._thread = None
            if self._subscribers and not self._closed:
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._produce, name="flex-deck-camera", daemon=True
                )
                self._thread.start()
            self._condition.notify_all()

    def _produce_http(self) -> None:
        with httpx.Client(
            base_url=self.robot_server_url,
            headers=_HEADERS,
            timeout=5,
            transport=self._transport,
        ) as client:
            while not self._stop.is_set():
                started = time.monotonic()
                response = client.post("/camera/picture")
                response.raise_for_status()
                self._publish(_jpeg(response.content), "http")
                remaining = self.http_interval - (time.monotonic() - started)
                if remaining > 0 and self._stop.wait(remaining):
                    return

    def _produce_ssh(self) -> None:
        client = self._ssh_factory()
        pid: int | None = None
        try:
            script = (
                "echo $$; exec v4l2-ctl "
                f"--device {shlex.quote(self.device)} "
                f"--set-fmt-video=width={self.width},height={self.height},pixelformat=MJPG "
                "--stream-mmap --stream-to=- --stream-count=0"
            )
            command = "sh -c " + shlex.quote(script)
            _, stdout, stderr = client.exec_command(command, timeout=5)
            first = stdout.readline()
            pid = int(first.decode("ascii") if isinstance(first, bytes) else first)
            channel = stdout.channel
            channel.settimeout(1)
            with self._condition:
                self._active_channel = channel
            data = bytearray()
            last_health = time.monotonic()
            while not self._stop.is_set():
                if time.monotonic() - last_health >= 2:
                    last_health = time.monotonic()
                    if self._robot_server_up():
                        return
                try:
                    chunk = channel.recv(65536)
                except TimeoutError:
                    continue
                if not chunk:
                    if self._stop.is_set():
                        return
                    raise RuntimeError(_closed_reason(channel, stderr))
                data.extend(chunk)
                if len(data) > 8 * 1024 * 1024:
                    raise RuntimeError("V4L2 camera frame exceeded 8 MiB")
                while True:
                    start = data.find(b"\xff\xd8")
                    end = data.find(b"\xff\xd9", start + 2)
                    if start < 0 or end < 0:
                        if start > 0:
                            del data[:start]
                        break
                    self._publish(bytes(data[start : end + 2]), "ssh")
                    del data[: end + 2]
        finally:
            with self._condition:
                self._active_channel = None
            if pid is not None:
                try:
                    client.exec_command(f"kill -TERM {pid}", timeout=2)
                except Exception:
                    pass
            client.close()


def _closed_reason(channel: Any, stderr: Any) -> str:
    """Say why the V4L2 channel closed: v4l2-ctl's exit status and stderr.

    Without them a wrong device path, a busy camera and an unsupported format
    all read the same ("channel closed"). The channel has already reached EOF,
    and its 1 s timeout bounds the reads; nothing here may raise.
    """
    reason = "V4L2 camera channel closed"
    try:
        if channel.exit_status_ready():
            reason += f" (exit {channel.recv_exit_status()})"
    except Exception:
        pass
    detail: Any = b""
    try:
        if stderr is not None:
            detail = stderr.read(2048)
    except Exception:
        pass
    if isinstance(detail, bytes):
        detail = detail.decode("utf-8", errors="replace")
    detail = " ".join(str(detail).split())[:300]
    return f"{reason}: {detail}" if detail else reason


MJPEG_MEDIA_TYPE = "multipart/x-mixed-replace; boundary=frame"
