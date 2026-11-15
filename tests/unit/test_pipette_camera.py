import httpx
import pytest
from fastapi.testclient import TestClient

from gibbie_server.api import create_app
from gibbie_server.config import DeviceConfig
from gibbie_server.pipette_camera import PipetteCamera

from conftest import config_for, device

TOKEN = "t" * 32


def _cfg(**extra) -> DeviceConfig:
    return DeviceConfig(name="Flex", kind="liquid_handler", probe="flex",
                        pipette_camera_url="http://127.0.0.1:8071", **extra)


def _camera(handler, monkeypatch, **extra) -> PipetteCamera:
    monkeypatch.setenv("GIBBIE_PIPETTE_CAMERA_TOKEN", TOKEN)
    return PipetteCamera(_cfg(**extra), transport=httpx.MockTransport(handler))


def _app(camera):
    cfg = config_for(dev=device("serial_port", kind="robot_arm"))
    return create_app(cfg, start_monitor=False, cameras={}, pipette_cameras={"gibbie_flex": camera})


def test_relays_the_requested_channel_with_the_server_side_token(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "multipart/x-mixed-replace; boundary=camera-frame"},
            content=b"--camera-frame\r\nContent-Type: image/jpeg\r\n\r\njpeg\r\n",
        )

    with TestClient(_app(_camera(handler, monkeypatch))) as client:
        response = client.get("/devices/gibbie_flex/pipette_camera/stream?stream=depth")
    assert response.status_code == 200
    assert response.headers["content-type"] == "multipart/x-mixed-replace; boundary=camera-frame"
    assert b"jpeg" in response.content
    request = seen[0]
    assert request.url.path == "/v1/cameras/d435i/stream.mjpg"
    assert request.url.params["stream"] == "depth"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


def test_upstream_refusal_is_an_honest_502_and_channels_are_allow_listed(monkeypatch):
    camera = _camera(lambda request: httpx.Response(409, text="camera busy"), monkeypatch)
    with TestClient(_app(camera)) as client:
        refused = client.get("/devices/gibbie_flex/pipette_camera/stream")
        assert refused.status_code == 502 and "409" in refused.json()["detail"]
        assert client.get("/devices/gibbie_flex/pipette_camera/stream?stream=ir").status_code == 422
        assert client.get("/devices/other/pipette_camera/stream").status_code == 404


def test_unreachable_camera_service_is_502(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused")

    with TestClient(_app(_camera(handler, monkeypatch))) as client:
        assert client.get("/devices/gibbie_flex/pipette_camera/stream").status_code == 502


def test_enabled_without_a_token_fails_at_startup(monkeypatch):
    monkeypatch.delenv("GIBBIE_PIPETTE_CAMERA_TOKEN", raising=False)
    with pytest.raises(ValueError, match="GIBBIE_PIPETTE_CAMERA_TOKEN"):
        PipetteCamera(_cfg())


def test_depth_readout_relays_the_pixel_with_the_token(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"pixel": [640, 360], "distance_m": 0.41, "valid_samples": 25})

    with TestClient(_app(_camera(handler, monkeypatch))) as client:
        reading = client.get("/devices/gibbie_flex/pipette_camera/depth?x=640&y=360")
        assert client.get("/devices/gibbie_flex/pipette_camera/depth?x=-1&y=0").status_code == 422
    assert reading.status_code == 200 and reading.json()["distance_m"] == 0.41
    assert seen[0].url.path == "/v1/cameras/d435i/depth"
    assert seen[0].url.params["x"] == "640" and seen[0].url.params["window"] == "5"
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_depth_refusal_is_502_with_the_reason(monkeypatch):
    camera = _camera(lambda r: httpx.Response(400, text="pixel (9999, 0) outside the map"), monkeypatch)
    with TestClient(_app(camera)) as client:
        refused = client.get("/devices/gibbie_flex/pipette_camera/depth?x=9999&y=0")
    assert refused.status_code == 502 and "outside" in refused.json()["detail"]
