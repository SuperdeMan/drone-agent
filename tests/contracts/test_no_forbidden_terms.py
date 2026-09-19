"""Terminology discipline (D018): manipulator and cockpit semantics must not leak into flight code.

术语纪律（D018）：机械臂与座舱语义不得渗入飞行代码。
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "drone_agent"

# Manipulator-arm state shapes and cockpit domain words. `vehicle` is deliberately NOT banned:
# PX4 and MAVLink use it for the aircraft itself (VehicleStatus, vehicle_command).
# 机械臂状态形状与座舱领域词。`vehicle` 刻意不禁：PX4 与 MAVLink 用它指飞行器本身。
FORBIDDEN = ("qpos", "qvel", "gripper", "ee_pose", "joint_targets", "cockpit", "cabin", "座舱")
_PATTERN = re.compile("|".join(re.escape(t) for t in FORBIDDEN), re.IGNORECASE)


def test_src_has_no_forbidden_terms():
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _PATTERN.search(line):
                offenders.append(f"{path.relative_to(SRC.parent)}:{lineno}: {line.strip()}")
    assert not offenders, "forbidden terms found:\n" + "\n".join(offenders)


def test_scanner_actually_detects(tmp_path):
    # Reverse check: the pattern must fire on a planted violation, or the test above proves nothing.
    # 反向验证：模式必须对故意植入的违规命中，否则上面的测试什么也证明不了。
    assert _PATTERN.search("obs.gripper_opening = 0.4")
    assert _PATTERN.search("# 座舱任务账本")
    assert not _PATTERN.search("vehicle_status = adapter.read()")
