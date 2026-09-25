"""The deployed v1 wire cannot silently change a field or RPC meaning.

已部署的 v1 wire 不得静默改变字段或 RPC 含义。
"""

import copy
import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = runpy.run_path(str(ROOT / "scripts/freeze_wire.py"))


def test_compiled_wire_preserves_frozen_signatures(tmp_path):
    descriptor = runpy.run_path(str(ROOT / "scripts/generate_proto.py"))["generate"](tmp_path)
    frozen = json.loads((ROOT / "proto/v1-wire-lock.json").read_text())
    TOOLS["check"](frozen, TOOLS["describe"](descriptor))


@pytest.mark.parametrize("section", ["messages", "enums", "services"])
def test_frozen_member_changes_are_rejected(section):
    frozen = json.loads((ROOT / "proto/v1-wire-lock.json").read_text())
    changed = copy.deepcopy(frozen)
    name = next(iter(changed[section]))
    member = next(iter(changed[section][name]))
    changed[section][name][member] = None
    with pytest.raises(ValueError, match="breaking frozen v1"):
        TOOLS["check"](frozen, changed)


def test_the_autonomy_protocol_is_frozen_since_m3_sitl():
    # D039: drone.autonomy.v1 was a draft until M3-SITL passed (2026-09-25); it is now locked like the other v1 packages.
    # D039：drone.autonomy.v1 在 M3-SITL 通过（2026-09-25）之前是草案；现在与其他 v1 包一样锁定。
    frozen = json.loads((ROOT / "proto/v1-wire-lock.json").read_text())
    for name in ("AutonomyFrame", "AuthorizedSetpoint", "AuthorizationRevoked", "EgressStatus", "LocalizationReport"):
        assert f"drone.autonomy.v1.{name}" in frozen["messages"], name
    with pytest.raises(ValueError, match="already frozen"):
        TOOLS["add_package"](copy.deepcopy(frozen), frozen, "drone.autonomy.v1")
    with pytest.raises(ValueError, match="not in the compiled wire"):
        TOOLS["add_package"](copy.deepcopy(frozen), frozen, "drone.nowhere.v1")
