"""FastAPI surface. Read-only; every handler serves from the monitor's cache."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sdl_lab_contract import EquipmentStatus, HealthResponse, ProbeResponse

from .config import Config
from .envelope import PROTOCOL_VERSION, device_envelope, service_envelope
from .monitor import Monitor
from .version import __version__

SERVICE_ID = "gibbie_server"


def create_app(config: Config, *, monitor: Monitor | None = None, start_monitor: bool = True) -> FastAPI:
    mon = monitor or Monitor(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_monitor:
            mon.start()
        try:
            yield
        finally:
            mon.stop()

    app = FastAPI(title="sdl2-gibbie-server", version=__version__, lifespan=lifespan)
    app.state.monitor = mon
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

    return app
