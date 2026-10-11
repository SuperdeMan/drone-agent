"""The pre-registered D084 comparison: whether per-leg corridors earn their place on the S0 ladders.

The rule reads ladder suite summaries of `eval/p3_world.py` (one per footprint model and world), the two S0 fault
matrices and the corridor P3 S1 receipt, and first checks that each input is what it claims: its part, rungs, seeds,
cases, footprint model and one common source commit. Safety: every case passed, every safety count and containment
violation is 0, under both models and in S1. Benefit: on the long-route rung the time tasks waited only on the
airspace falls by at least 30 % and by more than three times the noise of two box runs, while the drain time worsens
by at most 5 %; on the existing P3 rungs the drain time worsens by at most 10 %. Sums over seeds; the thresholds are
written into the pre-registration before any comparison run, from the two box noise runs only.

预登记的 D084 比较：航段走廊在 S0 阶梯上是否值得采用。规则读取 `eval/p3_world.py` 的阶梯汇总（每个模型与世界一份）、
两份 S0 故障矩阵与走廊下的 P3 S1 回执，先核对每份输入名副其实：部分、档位、种子、用例、航迹覆盖模型与同一源提交。
安全：两种模型与 S1 中每个用例通过，所有安全计数与轨迹违例为 0。收益：长航线档上任务只因空域等待的时长下降至少 30%，
且超过两次外包矩形运行之差（噪声）的三倍，排空时长变差不超过 5%；现有 P3 各档排空时长变差不超过 10%。按种子求和；
阈值只用两次外包矩形噪声运行算出，在任何比较运行之前写入预登记。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
WAIT_DROP = 0.30
NOISE_FACTOR = 3.0
LONG_DRAIN = 1.05
LADDER_DRAIN = 1.10
LONG_RUNG = "p6_long_l10"
SEEDS = (7, 19, 41)


def _suite(name: str) -> dict:
    return yaml.safe_load((ROOT / "configs/scenarios" / name).read_text(encoding="utf-8"))


def expected_cases() -> dict[str, set[tuple[str, int]]]:
    """The (scenario, seed) pairs each input must hold, from the committed suite files.

    每份输入必须包含的（场景，种子）对，取自已提交的场景集文件。
    """
    p3, long = _suite("p3_suite.yaml"), _suite("p6_airspace_suite.yaml")

    def cases(entries, default):
        return {(e["id"], seed) for e in entries for seed in e.get("seeds") or default}

    return {"long": cases([e for e in long["ladder"] if e["id"] == LONG_RUNG], long["seeds"]),
            "ladder": cases(p3["ladder"], p3["seeds"]), "matrix": cases(p3["s0"], p3["seeds"]),
            "s1": cases(p3["s1"], p3["seeds"])}


def rungs(summary: dict) -> dict[str, dict]:
    """Per rung, summed over seeds: airspace-only wait, drain time, cases, passes, safety counts and violations.

    按档、对种子求和：只因空域等待的时长、排空时长、用例数、通过数、安全计数与轨迹违例。
    """
    found: dict[str, dict] = {}
    for row in summary["results"]:
        metrics = row.get("metrics") or {}
        rung = found.setdefault(row["scenario"], {"wait_s": 0.0, "drain_s": 0.0, "cases": 0, "passed": 0,
                                                  "counts": 0, "violations": 0, "seeds": []})
        rung["wait_s"] = round(rung["wait_s"] + (metrics.get("airspace_wait_s") or {}).get("total", 0.0), 3)
        rung["drain_s"] = round(rung["drain_s"] + ((metrics.get("ladder") or {}).get("duration_s") or 0.0), 3)
        rung["cases"] += 1
        rung["passed"] += row.get("passed") is True
        rung["counts"] += sum((row.get("counts") or {}).values())
        rung["violations"] += (row.get("containment") or {}).get("violations", 1)
        rung["seeds"].append(row["seed"])
    return found


def safe(summary: dict) -> list[str]:
    """Problems that rule a summary out: a failed case, a nonzero count or violation, an unjudged containment.

    排除一份汇总的问题：失败用例、非零计数或违例、未做轨迹核对。
    """
    problems = [f"{row['scenario']}-{row['seed']}:not_passed" for row in summary["results"] if row.get("passed") is not True]
    problems += [f"{row['scenario']}-{row['seed']}:counts" for row in summary["results"]
                 if any((row.get("counts") or {}).values())]
    problems += [f"{row['scenario']}-{row['seed']}:containment" for row in summary["results"]
                 if (row.get("containment") or {}).get("violations", 1) != 0]
    if not summary["results"] or summary.get("status") != "passed":
        problems.append("suite_not_passed")
    return problems


def identity(summary: dict, *, kind: str, footprint: str) -> list[str]:
    """Whether a summary is the input it claims to be: part or layer, cases and footprint model.

    一份汇总是否名副其实：部分或层、用例与航迹覆盖模型。
    """
    problems = []
    cases = {(row["scenario"], row["seed"]) for row in summary["results"]}
    if len(cases) != len(summary["results"]) or cases != expected_cases()[kind]:
        problems.append(f"cases_differ_from_the_{kind}_suite")
    if summary.get("footprint", "box-v1") != footprint:
        problems.append(f"footprint_is_not_{footprint}")
    if kind == "s1":
        if summary.get("layer") != "S1":
            problems.append("not_an_s1_receipt")
    elif summary.get("part") != ("s0" if kind == "matrix" else "ladder"):
        problems.append("wrong_part")
    return problems


def thresholds(first: dict, second: dict) -> dict:
    """From two box runs of the long rung: the noise of the wait and of the drain, and the frozen factors.

    由长航线档的两次外包矩形运行得到：等待与排空的噪声，以及冻结的系数。
    """
    problems = identity(first, kind="long", footprint="box-v1") + identity(second, kind="long", footprint="box-v1")
    if first.get("source_sha") != second.get("source_sha"):
        problems.append("noise_runs_on_different_commits")
    if problems:
        raise ValueError(f"not two box runs of the long rung on one commit: {problems}")
    a, b = rungs(first)[LONG_RUNG], rungs(second)[LONG_RUNG]
    return {"rung": LONG_RUNG, "source_sha": first.get("source_sha"),
            "noise_wait_s": round(abs(a["wait_s"] - b["wait_s"]), 3),
            "noise_drain_s": round(abs(a["drain_s"] - b["drain_s"]), 3), "wait_drop": WAIT_DROP,
            "noise_factor": NOISE_FACTOR, "long_drain": LONG_DRAIN, "ladder_drain": LADDER_DRAIN,
            "noise_runs": [{"wait_s": a["wait_s"], "drain_s": a["drain_s"]},
                           {"wait_s": b["wait_s"], "drain_s": b["drain_s"]}]}


def decide(*, box_long: dict, corridor_long: dict, box_ladder: dict, corridor_ladder: dict, box_matrix: dict,
           corridor_matrix: dict, corridor_s1: dict, frozen: dict) -> dict:
    """Apply the pre-registered rule; `adopt` only when every input is what it claims, safety holds everywhere and
    every benefit criterion passes.

    应用预登记规则；只有每份输入名副其实、处处安全且每条收益判据都通过时才 `adopt`。
    """
    named = {"box_long": (box_long, "long", "box-v1"), "corridor_long": (corridor_long, "long", "corridor-v1"),
             "box_ladder": (box_ladder, "ladder", "box-v1"),
             "corridor_ladder": (corridor_ladder, "ladder", "corridor-v1"),
             "box_matrix": (box_matrix, "matrix", "box-v1"),
             "corridor_matrix": (corridor_matrix, "matrix", "corridor-v1"),
             "corridor_s1": (corridor_s1, "s1", "corridor-v1")}
    inputs = {name: identity(summary, kind=kind, footprint=model) for name, (summary, kind, model) in named.items()}
    commits = {name: summary.get("source_sha") for name, (summary, _, _) in named.items()}
    if len(set(commits.values())) != 1 or frozen.get("source_sha") not in set(commits.values()):
        inputs["commits"] = ["inputs_or_noise_runs_on_different_commits"]
    safety = {name: safe(summary) for name, (summary, _, _) in named.items()}
    box, corridor = rungs(box_long).get(LONG_RUNG), rungs(corridor_long).get(LONG_RUNG)
    criteria = {
        "inputs": {"status": "passed" if not any(inputs.values()) else "failed",
                   "problems": {k: v for k, v in inputs.items() if v}, "source_sha": commits},
        "safety": {"status": "passed" if not any(safety.values()) else "failed",
                   "problems": {k: v for k, v in safety.items() if v}},
    }
    if box is None or corridor is None:
        criteria["long_wait"] = {"status": "failed", "reason": "long rung missing"}
        return {"adopt": False, "criteria": criteria, "frozen": frozen}
    drop = box["wait_s"] - corridor["wait_s"]
    relative = frozen["wait_drop"] * box["wait_s"]
    noise = frozen["noise_factor"] * frozen["noise_wait_s"]
    criteria["long_wait"] = {"status": "passed" if box["wait_s"] > 0 and drop >= relative and drop > noise
                             else "failed", "box_s": box["wait_s"], "corridor_s": corridor["wait_s"],
                             "drop_s": round(drop, 3), "at_least_s": round(relative, 3),
                             "more_than_s": round(noise, 3)}
    criteria["long_drain"] = {"status": "passed" if corridor["drain_s"] <= frozen["long_drain"] * box["drain_s"]
                              else "failed", "box_s": box["drain_s"], "corridor_s": corridor["drain_s"],
                              "limit_s": round(frozen["long_drain"] * box["drain_s"], 3)}
    ladder_box, ladder_corridor = rungs(box_ladder), rungs(corridor_ladder)
    worse = {rung: {"box_s": numbers["drain_s"], "corridor_s": ladder_corridor.get(rung, {}).get("drain_s"),
                    "limit_s": round(frozen["ladder_drain"] * numbers["drain_s"], 3)}
             for rung, numbers in sorted(ladder_box.items())}
    criteria["ladder_drain"] = {
        "status": "passed" if worse and set(ladder_box) == set(ladder_corridor) and all(
            v["box_s"] > 0 and v["corridor_s"] is not None and v["corridor_s"] <= v["limit_s"]
            for v in worse.values()) else "failed",
        "rungs": worse}
    return {"adopt": all(c["status"] == "passed" for c in criteria.values()), "criteria": criteria,
            "frozen": frozen}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    noise = sub.add_parser("thresholds", help="freeze the thresholds from two box runs of the long rung")
    noise.add_argument("--first", type=Path, required=True)
    noise.add_argument("--second", type=Path, required=True)
    rule = sub.add_parser("decide", help="apply the pre-registered rule")
    for name in ("box-long", "corridor-long", "box-ladder", "corridor-ladder", "box-matrix", "corridor-matrix",
                 "corridor-s1", "frozen"):
        rule.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "thresholds":
        result = thresholds(_load(args.first), _load(args.second))
    else:
        frozen = _load(args.frozen)
        result = decide(box_long=_load(args.box_long), corridor_long=_load(args.corridor_long),
                        box_ladder=_load(args.box_ladder), corridor_ladder=_load(args.corridor_ladder),
                        box_matrix=_load(args.box_matrix), corridor_matrix=_load(args.corridor_matrix),
                        corridor_s1=_load(args.corridor_s1), frozen=frozen.get("thresholds", frozen))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
