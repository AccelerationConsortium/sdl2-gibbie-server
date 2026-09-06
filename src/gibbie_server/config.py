"""TOML configuration -> typed config.

One `[devices.<equipment_id>]` table per monitored device. Keys beyond the
common three (`name`, `kind`, `probe`) are probe-specific and reach the probe
through :meth:`DeviceConfig.option`, so adding a probe type needs no change here.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sdl_lab_contract import EquipmentKind

_KINDS = frozenset(get_args(EquipmentKind))


class ServiceConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8070
    poll_interval_s: float = Field(10.0, gt=0)
    # Consecutive failed probes before a device reads `unknown` (§2.1).
    unreachable_after: int = Field(2, ge=1)


class DeviceConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    kind: str
    probe: str

    @field_validator("kind")
    @classmethod
    def _kind_in_spec(cls, v: str) -> str:
        if v not in _KINDS:
            raise ValueError(f"kind {v!r} is not a STATUS_SPEC EquipmentKind: {sorted(_KINDS)}")
        return v

    def option(self, key: str, default: Any = None) -> Any:
        extra = self.model_extra or {}
        return extra.get(key, default)


class Config(BaseModel):
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    devices: dict[str, DeviceConfig]

    @field_validator("devices")
    @classmethod
    def _at_least_one(cls, v: dict[str, DeviceConfig]) -> dict[str, DeviceConfig]:
        if not v:
            raise ValueError("config declares no [devices.*] tables")
        return v


def load_config(path: str | Path) -> Config:
    with Path(path).open("rb") as f:
        return Config.model_validate(tomllib.load(f))
