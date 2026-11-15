import time

import httpx

from gibbie_server.camera import FlexCamera

from conftest import device


JPEG = b"\xff\xd8test-frame\xff\xd9"


def _cfg(**extra):
    return device(
        "flex",
        kind="liquid_handler",
        robot_server_url="http://flex:31950",
        ssh_alias="otflex",
        ssh_host="flex",
        ssh_key_file="key",
        camera_enabled=True,
        **extra,
    )


def test_http_mode_polls_official_picture_endpoint_only_while_viewed():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy"})
        if request.url.path == "/camera/picture":
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    camera = FlexCamera(_cfg(), transport=httpx.MockTransport(handler))
    stream = camera.multipart()
    try:
        part = next(stream)
        assert JPEG in part
        assert camera.component_status().state == "streaming"
        assert ("POST", "/camera/picture") in calls
    finally:
        stream.close()
        camera.close()
    assert camera.component_status().state == "off"


class _Channel:
    def __init__(self):
        self.chunks = [JPEG]
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self, size):
        if self.chunks:
            return self.chunks.pop(0)
        raise TimeoutError

    def close(self):
        self.closed = True


class _Stdout:
    def __init__(self, channel):
        self.channel = channel

    def readline(self):
        return b"4321\n"


class _SshClient:
    def __init__(self):
        self.channel = _Channel()
        self.commands = []
        self.closed = False

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        return None, _Stdout(self.channel), None

    def close(self):
        self.closed = True


def test_ssh_mode_uses_v4l2_channel_and_stops_its_exact_pid():
    transport = httpx.MockTransport(lambda request: httpx.Response(502))
    ssh = _SshClient()
    camera = FlexCamera(_cfg(), transport=transport, ssh_factory=lambda: ssh)
    stream = camera.multipart()
    try:
        assert JPEG in next(stream)
        assert camera.component_status().message == "deck camera streaming via ssh"
    finally:
        stream.close()
        camera.close()
    assert any("v4l2-ctl" in command for command in ssh.commands)
    assert any("kill -TERM 4321" in command for command in ssh.commands)
    assert ssh.closed


def test_invalid_http_picture_is_not_forwarded():
    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(200, content=b"not an image")

    camera = FlexCamera(_cfg(), transport=httpx.MockTransport(handler))
    stream = camera.multipart()
    try:
        # The producer retries; status truthfully reports the bad upstream.
        iterator = iter(stream)

        def consume():
            try:
                next(iterator)
            except StopIteration:
                pass

        thread = __import__("threading").Thread(target=consume, daemon=True)
        thread.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and camera.component_status().state != "unavailable":
            time.sleep(0.01)
        assert camera.component_status().state == "unavailable"
        assert "complete JPEG" in (camera.component_status().message or "")
    finally:
        camera.close()
        thread.join(timeout=2)
        stream.close()


def test_default_device_is_the_cameras_stable_link():
    from gibbie_server.camera import DEFAULT_CAMERA_DEVICE

    camera = FlexCamera(_cfg())
    assert camera.device == DEFAULT_CAMERA_DEVICE
    assert DEFAULT_CAMERA_DEVICE.startswith("/dev/v4l/by-id/") and DEFAULT_CAMERA_DEVICE.endswith("index0")
    assert FlexCamera(_cfg(camera_device="/dev/video9")).device == "/dev/video9"


class _FailingChannel(_Channel):
    """v4l2-ctl exits at once: EOF on stdout, an exit status, text on stderr."""

    def __init__(self):
        super().__init__()
        self.chunks = []

    def recv(self, size):
        return b""

    def exit_status_ready(self):
        return True

    def recv_exit_status(self):
        return 255


class _Stderr:
    def __init__(self, text):
        self.text = text

    def read(self, size=-1):
        return self.text


class _FailingSshClient(_SshClient):
    def __init__(self):
        super().__init__()
        self.channel = _FailingChannel()

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        if "v4l2-ctl" in command:
            return None, _Stdout(self.channel), _Stderr(b"Cannot open device /dev/ot_system_camera, exiting.\n")
        return None, _Stdout(self.channel), None


def test_a_v4l2_exit_reports_its_status_and_stderr():
    transport = httpx.MockTransport(lambda request: httpx.Response(502))
    camera = FlexCamera(_cfg(), transport=transport, ssh_factory=_FailingSshClient)
    stream = camera.multipart()
    iterator = iter(stream)

    def consume():
        try:
            next(iterator)
        except StopIteration:
            pass

    thread = __import__("threading").Thread(target=consume, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and camera.component_status().state != "unavailable":
            time.sleep(0.01)
        status = camera.component_status()
        assert status.state == "unavailable"
        assert "exit 255" in (status.message or "")
        assert "Cannot open device /dev/ot_system_camera, exiting." in (status.message or "")
    finally:
        camera.close()
        thread.join(timeout=2)
        stream.close()


class _ClosableChannel(_Channel):
    """Streams one frame, then blocks in recv like paramiko until its timeout,
    returning EOF at once when the channel is closed under it."""

    def __init__(self):
        super().__init__()
        self._closing = __import__("threading").Event()

    def recv(self, size):
        if self.chunks:
            return self.chunks.pop(0)
        if self._closing.wait(1):
            return b""
        raise TimeoutError

    def close(self):
        self.closed = True
        self._closing.set()


def test_the_last_viewer_leaving_is_not_logged_as_a_failure(caplog):
    caplog.set_level("DEBUG", logger="gibbie_server.camera")
    transport = httpx.MockTransport(lambda request: httpx.Response(502))
    ssh = _SshClient()
    ssh.channel = _ClosableChannel()
    camera = FlexCamera(_cfg(), transport=transport, ssh_factory=lambda: ssh)
    stream = camera.multipart()
    try:
        assert JPEG in next(stream)
    finally:
        stream.close()
        camera.close()
    assert ssh.channel.closed and ssh.closed
    warnings = [r for r in caplog.records if r.name == "gibbie_server.camera" and r.levelname == "WARNING"]
    assert warnings == []
    assert camera.component_status().state == "off"
