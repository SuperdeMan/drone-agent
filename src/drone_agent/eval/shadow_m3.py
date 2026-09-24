"""Judge-side shadow report for an M3 flight: replay the shadow policy, then score it against truth (WP-M3-18, D013).

`autonomy.shadow.replay` feeds the shadow-stage policy the inputs the deterministic planner saw during the flight and
never touches truth. This judge-side step adds what only the judge may know: for every shadow proposal, whether the
straight path from the recorded position to the proposed target keeps the truth clearance to every obstacle surface —
including the crate the registry does not list — and stays inside the geofence. The report is written beside, never
into, the case result; shadow outputs are archived separately from execution results (08-evaluation §6).

M3 飞行的裁判侧影子报告：先回放影子策略，再对照真值打分（WP-M3-18，D013）。

`autonomy.shadow.replay` 把飞行中确定性规划器所见的输入交给影子阶段策略，从不接触真值。本裁判侧步骤补上只有裁判能知道的
部分：对每个影子提议，判断从记录位置到提议目标的直线路径是否与每个障碍表面（含登记表未列出的箱体）保持真值净距并留在
围栏内。报告写在用例结果旁边而不写进结果；影子输出与执行结果分开归档（08-evaluation §6）。
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import yaml

from drone_agent.autonomy.shadow import POLICIES, replay
from drone_agent.eval.judge_m3 import clearance, truth_obstacles
from drone_agent.mission.registry import M3_SCENE, Registry
from drone_agent.runtime.ledger import canonical


def approved(position, target, obstacles: dict, registry: Registry, *, samples: int = 10) -> bool:
    """Truth approval of one proposal: every sample of the straight path keeps the clearance and the fence.

    对一个提议的真值认可：直线路径上每个采样点都保持净距并在围栏内。
    """
    threshold = registry.data["thresholds"]["min_clearance_m"]
    for k in range(samples + 1):
        point = [p + (t - p) * k / samples for p, t in zip(position, target, strict=True)]
        if not registry.inside(point):
            return False
        if any(clearance(point, spec) < threshold for spec in obstacles.values()):
            return False
    return True


def shadow_report(run: Path, root: Path, policy: str = "goal_direct_baseline") -> dict:
    registry = Registry(root, scene=root / M3_SCENE)
    scene = yaml.safe_load((root / M3_SCENE).read_text(encoding="utf-8"))
    report = replay(run, scene, POLICIES[policy]())
    obstacles = truth_obstacles(root)
    verdicts = [approved(row["position"], row["shadow_carrot"], obstacles, registry) for row in report["rows"]]
    report["judge_approved"] = sum(verdicts)
    report["judge_approval_rate"] = round(sum(verdicts) / len(verdicts), 3) if verdicts else None
    report["deterministic_judge_approval_rate"] = None
    references = [row for row in report["rows"] if row["deterministic_carrot"] is not None]
    if references:
        reference = [approved(row["position"], row["deterministic_carrot"], obstacles, registry) for row in references]
        report["deterministic_judge_approval_rate"] = round(sum(reference) / len(reference), 3)
    report["executed"] = 0
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--root", type=Path, default=Path("/workspace"))
    parser.add_argument("--policy", choices=sorted(POLICIES), default="goal_direct_baseline")
    parser.add_argument("--output", type=Path, default=Path("/output/shadow.json"))
    args = parser.parse_args()
    report = shadow_report(args.run, args.root, args.policy)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical({k: (None if isinstance(v, float) and not math.isfinite(v) else v)
                                       for k, v in report.items()}))
    print(json.dumps({key: report.get(key) for key in ("cycles", "envelope_violation_rate", "judge_approval_rate",
                                                       "deterministic_judge_approval_rate", "executed")}))


if __name__ == "__main__":
    main()
