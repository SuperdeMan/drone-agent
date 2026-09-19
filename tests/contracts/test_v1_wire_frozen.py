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
