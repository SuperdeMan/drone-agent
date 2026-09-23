"""Estimate per-skill SITL energy fractions from recorded M1 runs (WP-M2-07, D034).

The PX4 SITL battery drains linearly while armed, stops at a floor (SIM_BAT_MIN_PCT) and resets to full
after disarming, so a raw start/end difference is only trustworthy while neither boundary is involved.
This script therefore fits the drain rate on uncensored steps, measures each skill's duration on every
completed step, and reports mean = rate_mean x duration_mean and a conservative upper bound =
rate_p95 x duration_p95 (the product of two 95th percentiles; no further margin is stacked on top,
because summing per-skill bounds along a mission already compounds the conservatism). Steps are
delimited by the executive's own journal; the battery value comes from the guardian's recorded
observations. The result is simulation-only by construction.

从已记录的 M1 运行估计各技能的 SITL 能耗分数（WP-M2-07，D034）。

PX4 SITL 电池在解锁期间线性放电，降到下限（SIM_BAT_MIN_PCT）后不再下降，上锁后重置为满电，因此
只有两端都不受这些边界影响时，起止差值才可信。本脚本在未截断的步骤上拟合放电率，在每个完成的步骤
上测量技能时长，报告 均值 = 平均放电率 × 平均时长，保守上界 = 放电率 p95 × 时长 p95（两个 95 分位之积；
不再叠加额外余量，因为沿任务累加各技能上界本身已经叠加了保守性）。
步骤边界取自 executive 自己的账本，电量取自 guardian 记录的观测。结果按构造只适用于仿真。
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from drone_agent.runtime.ledger import read_log  # noqa: E402
from drone_agent.runtime.recording import replay  # noqa: E402

FLOOR = 0.52  # at or below this the SITL floor (50 %) may have clipped the drain / 低于此值可能已被 50% 下限截断


def quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def battery_series(run: Path) -> list[tuple[datetime, float]]:
    series = []
    for topic, data in replay(run / "guardian.mcap"):
        if topic == "flight/observation" and data.get("battery_fraction") is not None:
            series.append((datetime.fromisoformat(data["timestamp"]), float(data["battery_fraction"])))
    return series


def value_at(series, moment: datetime) -> float | None:
    best = None
    for stamp, value in series:
        if stamp > moment:
            break
        best = value
    return best


def steps(run: Path) -> list[dict]:
    rows = read_log(run / "executive.jsonl")
    started, result = {}, []
    for row in rows:
        data = row["data"]
        if row["kind"] == "command_submitted" and data.get("capture_attempt") is None:
            started.setdefault(data["envelope"]["key"]["step_id"], datetime.fromisoformat(row["timestamp"]))
        elif row["kind"] == "step_outcome":
            outcome = data["outcome"]
            step = outcome["step_id"]
            if step in started:
                result.append({
                    "step": step,
                    "start": started[step],
                    "end": datetime.fromisoformat(row["timestamp"]),
                    "completed": outcome["execution_status"] == "succeeded" and outcome["effect_verdict"] == "verified",
                })
    return result


def estimate(run_root: Path) -> dict:
    rates, durations, runs = [], {}, []
    for case in sorted(p for p in run_root.iterdir() if (p / "aircraft/executive.jsonl").is_file()):
        aircraft = case / "aircraft"
        series = battery_series(aircraft)
        if not series:
            continue
        runs.append(case.name)
        for item in steps(aircraft):
            if not item["completed"]:
                continue
            seconds = (item["end"] - item["start"]).total_seconds()
            durations.setdefault(item["step"], []).append(seconds)
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
    skills = {}
    for step, values in sorted(durations.items()):
        mean = min(1.0, rate_mean * statistics.fmean(values))
        upper = min(1.0, rate_upper * quantile(values, 0.95))
        skills[step] = {"samples": len(values), "duration_mean_s": round(statistics.fmean(values), 3),
                        "duration_p95_s": round(quantile(values, 0.95), 3), "mean_fraction": round(mean, 4),
                        "upper_fraction": round(max(upper, mean), 4)}
    # inspect_asset has no M1 flights: compose its approach from fly_route and its capture from capture_image.
    # inspect_asset 没有 M1 飞行：接近段取 fly_route，拍摄段取 capture_image。
    if "fly_route" in durations and "capture_image" in durations:
        composed = [a + b for a, b in zip(durations["fly_route"], durations["capture_image"], strict=False)]
        mean = min(1.0, rate_mean * statistics.fmean(composed))
        skills["inspect_asset"] = {
            "samples": len(composed), "duration_mean_s": round(statistics.fmean(composed), 3),
            "duration_p95_s": round(quantile(composed, 0.95), 3), "mean_fraction": round(mean, 4),
            "upper_fraction": round(max(min(1.0, rate_upper * quantile(composed, 0.95)), mean), 4),
            "composed_from": ["fly_route", "capture_image"],
        }
    receipt = run_root / "receipt.json"
    revision = json.loads(receipt.read_text(encoding="utf-8")).get("source_sha", "") if receipt.is_file() else ""
    return {
        "schema_version": "0.1.0",
        "basis": "sim_only",
        "source_deployment": run_root.parent.name,
        "source_run": run_root.name,
        "source_revision": revision,
        "method": (f"PX4 SITL linear drain fitted on {len(rates)} uncensored steps; mean=rate_mean*duration_mean, "
                   "upper=rate_p95*duration_p95"),
        "drain_rate_per_s": {"mean": round(rate_mean, 5), "p95": round(rate_upper, 5), "samples": len(rates)},
        "runs": runs,
        "skills": skills,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path, help="fetched run directory, e.g. runs/<deployment>/m1-<run>")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = estimate(args.run_root)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
