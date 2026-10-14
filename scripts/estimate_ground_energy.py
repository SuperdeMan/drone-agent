"""Estimate per-skill SITL energy fractions of the X1 ground skills from recorded S1 runs (D088).

The same method as scripts/estimate_energy.py (WP-M2-07, D034): the PX4 SITL battery drains linearly while armed and
resets after disarming, so the drain rate is fitted on uncensored completed steps only, every completed step gives a
duration, mean = rate_mean x duration_mean and the upper bound = rate_p95 x duration_p95. Steps are delimited by the
executive's own journal, the battery comes from the guardian's recorded observations, and each step id is mapped to its
skill through the approved package (a recheck step is named after its asset). The result is simulation-only.

用录制的 S1 运行估计 X1 地面技能的 SITL 能耗分数（D088）。方法与 scripts/estimate_energy.py 相同（WP-M2-07，D034）：PX4 SITL
电池在解锁期间线性放电、上锁后重置，因此只在未截断的完成步骤上拟合放电率；每个完成步骤给出一个时长；均值 = 平均放电率 ×
平均时长，上界 = 放电率 p95 × 时长 p95。步骤边界取自 executive 自己的账本，电量取自 guardian 记录的观测，步骤 id 经已批准的
任务包映射到技能（复核步骤以资产命名）。结果只适用于仿真。
"""

from __future__ import annotations

import argparse
import json
import runpy
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = runpy.run_path(str(ROOT / "scripts/estimate_energy.py"))
battery_series, value_at, steps, quantile = (BASE[k] for k in ("battery_series", "value_at", "steps", "quantile"))
FLOOR = BASE["FLOOR"]


def flights(root: Path) -> list[tuple[Path, dict]]:
    """Every rover version with a journal and its approved package. / 每个带日志与已批准任务包的地面车版本。"""
    found = []
    for executive in sorted(root.rglob("robots/ugv_01/aircraft/m-*/v*/executive.jsonl")):
        flight = executive.parent
        package = flight.parents[2] / "inbox/history" / f"{flight.parent.name}-{flight.name}.json"
        if package.is_file():
            nodes = json.loads(package.read_text(encoding="utf-8"))["nodes"]
            found.append((flight, {node["task_id"]: node["skill_id"].removeprefix("skill.ground.") for node in nodes}))
    return found


def estimate(root: Path) -> dict:
    rates, durations, used = [], {}, []
    for flight, skills in flights(root):
        series = battery_series(flight)
        if not series:
            continue
        used.append(str(flight.relative_to(root)).replace("\\", "/"))
        for item in steps(flight):
            if not item["completed"]:
                continue
            skill = skills.get(item["step"])
            seconds = (item["end"] - item["start"]).total_seconds()
            durations.setdefault(skill, []).append(seconds)
            before, after = value_at(series, item["start"]), value_at(series, item["end"])
            if before is None or after is None or seconds < 5:
                continue
            # Censored: the floor may have clipped it, or disarming reset the battery. / 截断：下限或上锁重置。
            if after > before or after <= FLOOR or before <= FLOOR:
                continue
            rates.append((before - after) / seconds)
    if not rates:
        raise ValueError("no uncensored armed steps to fit a drain rate")
    rate_mean, rate_upper = statistics.fmean(rates), quantile(rates, 0.95)
    found = {}
    for skill, values in sorted(durations.items()):
        mean = min(1.0, rate_mean * statistics.fmean(values))
        upper = min(1.0, rate_upper * quantile(values, 0.95))
        found[skill] = {"samples": len(values), "duration_mean_s": round(statistics.fmean(values), 3),
                        "duration_p95_s": round(quantile(values, 0.95), 3), "mean_fraction": round(mean, 4),
                        "upper_fraction": round(max(upper, mean), 4)}
    return {"schema_version": "0.1.0", "basis": "sim_only",
            "method": (f"PX4 SITL linear drain fitted on {len(rates)} uncensored ground steps; "
                       "mean=rate_mean*duration_mean, upper=rate_p95*duration_p95"),
            "drain_rate_per_s": {"mean": round(rate_mean, 6), "p95": round(rate_upper, 6), "samples": len(rates)},
            "flights": used, "skills": found}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="fetched X1 S1 case directories")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    text = json.dumps(estimate(args.root), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8", newline="\n")
    print(text)


if __name__ == "__main__":
    main()
