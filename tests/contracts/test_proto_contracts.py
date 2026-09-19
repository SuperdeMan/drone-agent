"""Compile wire contracts and verify coverage, safe defaults and transport boundaries.

编译 wire 契约，检查字段覆盖、安全缺省值与传输边界。
"""

import copy
import json
import runpy
from pathlib import Path

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

ROOT = Path(__file__).resolve().parents[2]
# Tool scripts are not runtime packages; load them without changing importlib-mode paths.
# 工具脚本不是运行时包；加载它们时不改变 importlib 模式下的导入路径。
FIELD_TOOLS = runpy.run_path(str(ROOT / "scripts/generate_contract_fields.py"))
INVENTORY = FIELD_TOOLS["INVENTORY"]
SHARED_PROTO = FIELD_TOOLS["SHARED_PROTO"]
build_inventory = FIELD_TOOLS["build_inventory"]
check_compatibility = FIELD_TOOLS["check_compatibility"]
render_proto = FIELD_TOOLS["render_proto"]
generate = runpy.run_path(str(ROOT / "scripts/generate_proto.py"))["generate"]


@pytest.fixture(scope="module")
def pool(tmp_path_factory):
    descriptor = generate(tmp_path_factory.mktemp("proto"))
    files = descriptor_pb2.FileDescriptorSet.FromString(descriptor.read_bytes())
    result = descriptor_pool.DescriptorPool()
    for file in files.file:
        result.Add(file)
    return result


def test_model_inventory_and_shared_wire_are_current():
    inventory = build_inventory()
    assert json.loads(INVENTORY.read_text(encoding="utf-8")) == inventory
    assert SHARED_PROTO.read_text(encoding="utf-8") == render_proto(inventory)


def test_every_model_field_is_in_compiled_wire(pool):
    for name, fields in build_inventory()["models"].items():
        message = pool.FindMessageTypeByName(f"drone.contracts.v1.{name}")
        assert set(message.fields_by_name) == {field["name"] for field in fields}
        for field in fields:
            actual = message.fields_by_name[field["name"]]
            assert actual.number == field["number"]
            assert actual.is_repeated == (field["cardinality"] == "repeated")
            assert actual.has_presence or actual.is_repeated


def test_unset_verdicts_do_not_default_to_success_or_permission(pool):
    cls = message_factory.GetMessageClass(pool.FindMessageTypeByName("drone.contracts.v1.StepOutcome"))
    empty = cls.FromString(b"")
    for name in ("execution_status", "effect_verdict", "safety_verdict"):
        assert not empty.HasField(name)
        enum = empty.DESCRIPTOR.fields_by_name[name].enum_type
        assert enum.values_by_number[getattr(empty, name)].name.endswith("_UNSPECIFIED")


def test_empty_reconciliation_never_means_not_received(pool):
    cls = message_factory.GetMessageClass(pool.FindMessageTypeByName("drone.control.v1.CommandReconciliation"))
    result = cls.FromString(b"")
    assert result.receipt_status == 0
    assert not result.HasField("receipt_status")
    assert not result.HasField("outcome")


def test_control_roundtrip_preserves_zero_presence_and_large_integer(pool):
    cls = message_factory.GetMessageClass(pool.FindMessageTypeByName("drone.contracts.v1.ControlCommandEnvelope"))
    original = cls(command_seq=0, payload=b'{"route_id":9007199254740993}')
    original.key.lease_epoch = 9007199254740993
    restored = cls.FromString(original.SerializeToString())
    assert restored.HasField("command_seq") and restored.command_seq == 0
    assert restored.key.lease_epoch == 9007199254740993
    assert json.loads(restored.payload)["route_id"] == 9007199254740993


def test_fleet_service_cannot_accept_control_envelopes(pool):
    service = pool.FindServiceByName("drone.fleet.v1.FleetTransport")
    pending = [method.input_type for method in service.methods]
    seen = set()
    while pending:
        message = pending.pop()
        if message.full_name in seen:
            continue
        seen.add(message.full_name)
        pending.extend(field.message_type for field in message.fields if field.message_type)
    assert "drone.contracts.v1.ControlCommandEnvelope" not in seen
    assert "drone.contracts.v1.TaskLease" not in seen
    assert all("kill" not in method.name.lower() for method in service.methods)


def test_field_or_enum_renumbering_is_rejected():
    original = build_inventory()
    changed = copy.deepcopy(original)
    changed["models"]["TaskLease"][0]["number"] += 1
    with pytest.raises(ValueError, match="breaking wire"):
        check_compatibility(original, changed)
    changed = copy.deepcopy(original)
    changed["enums"]["EffectVerdict"]["EFFECT_VERDICT_VERIFIED"] = 0
    with pytest.raises(ValueError, match="breaking enum"):
        check_compatibility(original, changed)
