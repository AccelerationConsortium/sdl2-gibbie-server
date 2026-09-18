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
