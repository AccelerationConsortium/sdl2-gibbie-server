import pytest
from pydantic import ValidationError

from gibbie_server.config import Config, DeviceConfig
from gibbie_server.probes import PROBES, build_probe


def test_example_config_loads_and_every_probe_type_is_registered(example_config):
    assert len(example_config.devices) == 5
    for device_id, cfg in example_config.devices.items():
        assert cfg.probe in PROBES, device_id
        build_probe(device_id, cfg)  # constructs without touching hardware


def test_kind_must_be_a_spec_kind():
    with pytest.raises(ValidationError, match="EquipmentKind"):
        DeviceConfig(name="x", kind="balance", probe="http_endpoint")


def test_probe_specific_options_are_kept():
    cfg = DeviceConfig(name="x", kind="other", probe="serial_port", port="COM9")
    assert cfg.option("port") == "COM9"
    assert cfg.option("missing", 1) == 1


def test_config_needs_devices():
    with pytest.raises(ValidationError, match="no \\[devices"):
        Config(devices={})
