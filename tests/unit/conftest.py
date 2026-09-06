from pathlib import Path

import pytest

from gibbie_server.config import Config, DeviceConfig, ServiceConfig, load_config

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def example_config() -> Config:
    return load_config(ROOT / "config.example.toml")


def device(probe: str, kind: str = "other", **opts) -> DeviceConfig:
    return DeviceConfig(name=f"test {probe}", kind=kind, probe=probe, **opts)


def config_for(**devices: DeviceConfig) -> Config:
    return Config(service=ServiceConfig(poll_interval_s=1, unreachable_after=2), devices=devices)
