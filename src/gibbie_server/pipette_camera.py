"""Read-only relay for the pipette camera (the Gibbie D435i).

The D435i belongs to sdl-camera-server on this PC, which binds loopback and
requires a bearer token. This relay re-serves its MJPEG color or depth stream
on the tailnet-facing gibbie-server port so the dashboard's camera broker can
reach it, plus its point-depth readout, and keeps the token on this machine. One upstream request per
viewer; nothing is requested until a viewer connects, and the upstream closes
when the viewer leaves.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx

from .config import DeviceConfig

STREAMS = ("color", "depth")


class PipetteCameraUnavailable(Exception):
    """The upstream camera service refused or could not be reached."""


class PipetteCamera:
    def __init__(self, cfg: DeviceConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = str(cfg.option("pipette_camera_url")).rstrip("/")
        self.camera_id = str(cfg.option("pipette_camera_id", "d435i"))
        self.fps = float(cfg.option("pipette_camera_fps", 10))
        if not 0.5 <= self.fps <= 30:
            raise ValueError("pipette_camera_fps must be between 0.5 and 30")
        token_env = str(cfg.option("pipette_camera_token_env", "GIBBIE_PIPETTE_CAMERA_TOKEN"))
        self.token = os.environ.get(token_env, "")
        if not self.token:
            raise ValueError(f"pipette camera enabled but {token_env} is not set")
        self._transport = transport

    async def open(self, stream: str) -> tuple[str, AsyncIterator[bytes]]:
        """Connect upstream; return its content type and a byte iterator.

        Raises PipetteCameraUnavailable before any byte is sent, so the
        caller can answer with an honest error status instead of an empty
        200 stream.
        """
        if stream not in STREAMS:
            raise ValueError(f"stream must be one of {STREAMS}")
        client = httpx.AsyncClient(
            base_url=self.url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=httpx.Timeout(10, read=15),
            transport=self._transport,
        )
        try:
            response = await client.send(
                client.build_request(
                    "GET",
                    f"/v1/cameras/{self.camera_id}/stream.mjpg",
                    params={"stream": stream, "fps": self.fps},
                ),
                stream=True,
            )
        except httpx.HTTPError as exc:
            await client.aclose()
            raise PipetteCameraUnavailable(f"camera service unreachable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            body = (await response.aread())[:300].decode("utf-8", "replace")
            await response.aclose()
            await client.aclose()
            raise PipetteCameraUnavailable(f"camera service answered {response.status_code}: {body}")

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        return response.headers["content-type"], body()

    async def depth_at(self, x: int, y: int) -> dict:
        """Median metric depth of a 5x5 patch at pixel (x, y) of the stream.

        Depth is aligned to color upstream, so the coordinates are the same
        for the color and depth pictures.
        """
        async with httpx.AsyncClient(
            base_url=self.url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=5,
            transport=self._transport,
        ) as client:
            try:
                response = await client.get(
                    f"/v1/cameras/{self.camera_id}/depth", params={"x": x, "y": y, "window": 5}
                )
            except httpx.HTTPError as exc:
                raise PipetteCameraUnavailable(f"camera service unreachable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise PipetteCameraUnavailable(
                f"camera service answered {response.status_code}: {response.text[:300]}"
            )
        return response.json()
