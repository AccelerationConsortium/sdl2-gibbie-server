"""FastAPI surface: cached status plus an opt-in read-only camera stream."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sdl_lab_contract import EquipmentStatus, HealthResponse, ProbeResponse

from .camera import FlexCamera, MJPEG_MEDIA_TYPE
from .config import Config
from .envelope import PROTOCOL_VERSION, device_envelope, service_envelope
from .monitor import Monitor
from .pipette_camera import PipetteCamera, PipetteCameraUnavailable
from .probes.flex import FlexProbe
from .version import __version__

SERVICE_ID = "gibbie_server"


def create_app(
    config: Config,
    *,
    monitor: Monitor | None = None,
    start_monitor: bool = True,
    cameras: dict[str, FlexCamera] | None = None,
    pipette_cameras: dict[str, PipetteCamera] | None = None,
) -> FastAPI:
    mon = monitor or Monitor(config)
    camera_map = cameras if cameras is not None else {
        device_id: FlexCamera(rec.cfg)
        for device_id, rec in mon.records.items()
        if isinstance(rec.probe, FlexProbe) and rec.cfg.option("camera_enabled", False)
    }
    pipette_map = pipette_cameras if pipette_cameras is not None else {
        device_id: PipetteCamera(rec.cfg)
        for device_id, rec in mon.records.items()
        if isinstance(rec.probe, FlexProbe) and rec.cfg.option("pipette_camera_url")
    }
    for device_id, camera in camera_map.items():
        rec = mon.records.get(device_id)
        if rec is not None and isinstance(rec.probe, FlexProbe):
            rec.probe.set_camera_status_provider(camera.component_status)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_monitor:
            mon.start()
        try:
            yield
        finally:
            mon.stop()
            for camera in camera_map.values():
                camera.close()

    app = FastAPI(title="sdl2-gibbie-server", version=__version__, lifespan=lifespan)
    app.state.monitor = mon
    app.state.cameras = camera_map
    app.state.pipette_cameras = pipette_map
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

    @app.get("/", response_model=ProbeResponse)
    def root() -> ProbeResponse:
        return ProbeResponse(equipment_id=SERVICE_ID, equipment_name="Gibbie Monitor", protocol_version=PROTOCOL_VERSION)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/status", response_model=EquipmentStatus)
    def status() -> EquipmentStatus:
        return service_envelope(mon, equipment_id=SERVICE_ID)

    @app.get("/devices")
    def devices() -> dict:
        return {
            "devices": [
                {"id": rec.device_id, "name": rec.cfg.name, "kind": rec.cfg.kind, "probe": rec.probe.probe_type,
                 "status_path": f"/devices/{rec.device_id}/status"}
                for rec in mon.records.values()
            ]
        }

    def _record(device_id: str):
        try:
            return mon.records[device_id]
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown device {device_id!r}") from None

    @app.get("/devices/{device_id}", response_model=ProbeResponse)
    def device_probe(device_id: str) -> ProbeResponse:
        rec = _record(device_id)
        return ProbeResponse(equipment_id=rec.device_id, equipment_name=rec.cfg.name, protocol_version=PROTOCOL_VERSION)

    @app.get("/devices/{device_id}/status", response_model=EquipmentStatus)
    def device_status(device_id: str) -> EquipmentStatus:
        return device_envelope(_record(device_id), mon)

    @app.get("/devices/{device_id}/camera/stream")
    def camera_stream(device_id: str):
        camera = camera_map.get(device_id)
        if camera is None:
            raise HTTPException(status_code=404, detail=f"no camera configured for {device_id!r}")
        return StreamingResponse(
            camera.multipart(),
            media_type=MJPEG_MEDIA_TYPE,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/devices/{device_id}/pipette_camera/stream")
    async def pipette_camera_stream(
        device_id: str, stream: str = Query("color", pattern="^(color|depth)$")
    ):
        camera = pipette_map.get(device_id)
        if camera is None:
            raise HTTPException(status_code=404, detail=f"no pipette camera configured for {device_id!r}")
        try:
            media_type, body = await camera.open(stream)
        except PipetteCameraUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        return StreamingResponse(body, media_type=media_type, headers={"Cache-Control": "no-store"})

    @app.get("/devices/{device_id}/pipette_camera/depth")
    async def pipette_camera_depth(device_id: str, x: int = Query(ge=0), y: int = Query(ge=0)):
        camera = pipette_map.get(device_id)
        if camera is None:
            raise HTTPException(status_code=404, detail=f"no pipette camera configured for {device_id!r}")
        try:
            return await camera.depth_at(x, y)
        except PipetteCameraUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    return app
