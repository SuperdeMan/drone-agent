"""Every contract object is versioned and tolerant to additive fields (backward compatibility rule §0).

每个契约对象都带版本号并容忍新增字段（向后兼容规则 §0）。
"""

import inspect

import pytest

import drone_agent.contracts as contracts
from drone_agent.contracts import CONTRACT_VERSION, ContractModel, MissionSpec, RobotStatus
from tests.contracts.factories import NOW, mission_spec

# Every ContractModel subclass exported by the package. / 包导出的全部 ContractModel 子类。
CONTRACT_MODELS = [
    obj
    for _, obj in inspect.getmembers(contracts, inspect.isclass)
    if issubclass(obj, ContractModel) and obj is not ContractModel
]


def test_contract_model_inventory_is_not_empty():
    assert len(CONTRACT_MODELS) >= 30


@pytest.mark.parametrize("model", CONTRACT_MODELS, ids=lambda m: m.__name__)
def test_every_contract_carries_schema_version(model):
    assert "schema_version" in model.model_fields
    assert model.model_fields["schema_version"].default == CONTRACT_VERSION


@pytest.mark.parametrize("model", CONTRACT_MODELS, ids=lambda m: m.__name__)
def test_unknown_fields_are_ignored_not_fatal(model):
    assert model.model_config.get("extra") == "ignore"


def test_newer_mission_spec_with_extra_fields_parses_on_older_reader():
    # A newer producer adds fields; an older consumer must still parse. / 新生产者加字段，旧消费者仍能解析。
    payload = mission_spec().model_dump(mode="json")
    payload["schema_version"] = "0.2.0"
    payload["brand_new_field"] = {"nested": True}
    payload["tasks"][0]["future_hint"] = "ignored"
    spec = MissionSpec.model_validate(payload)
    assert spec.schema_version == "0.2.0"
    assert not hasattr(spec, "brand_new_field")


def test_robot_status_parses_from_minimal_json_with_extras():
    status = RobotStatus.model_validate(
        {
            "robot_id": "uav_01",
            "timestamp": NOW.isoformat(),
            "energy": {"remaining_fraction": 0.8},
            "localization": {"healthy": True, "fix_type": "rtk_fixed"},
            "comms": {"uplink_ok": True},
            "vendor_specific": {"foo": 1},
        }
    )
    assert status.energy.above_reserve
    assert status.control_mode.value == "none"
