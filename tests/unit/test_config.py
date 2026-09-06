import pytest
from pydantic import ValidationError

from gibbie_server.config import Config, DeviceConfig
from gibbie_server.probes import PROBES, build_probe


def test_example_config_loads_and_every_probe_type_is_registered(example_config):
    assert len(example_config.devices) == 5
    for device_id, cfg in example_config.devices.items():
        assert cfg.probe in PROBES, device_id
        build_probe(device_id, cfg)  # constructs without touching hardware


def test_process_chemistry_config_loads_on_the_same_code(process_chemistry_config):
    """The second bench is a config file, not a fork: every probe it names is
    registered here and constructs without touching hardware."""
    cfg = process_chemistry_config
    assert set(cfg.devices) == {"lle_ur5_arm", "lle_xpr_balance", "lle_easymax", "lle_hplc", "lle_ph_unit"}
    for device_id, device_cfg in cfg.devices.items():
        assert device_cfg.probe in PROBES, device_id
        build_probe(device_id, device_cfg)


def test_the_two_benches_share_no_equipment_ids(example_config, process_chemistry_config):
    """Both instances feed one dashboard registry, so a collision would make two
    devices fight over one tile."""
    assert not set(example_config.devices) & set(process_chemistry_config.devices)


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
