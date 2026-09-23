"""Run the versioned adversarial planning corpora (WP-M2-09, 08-evaluation §8).

The deterministic corpus patches directly constructed MissionSpecs onto a nominal inspection and runs
them through intake, the compiler and admission. The report records, per case, the stage that blocked
it and the issue codes; a case passes only when it was blocked at the expected stage with at least the
expected codes. Nothing here talks to a robot: an admitted package is itself a failure.

运行版本化的对抗性规划语料（WP-M2-09，08-evaluation §8）。

确定性语料把直接构造的 MissionSpec 叠加到标称巡检上，经接收、编译与准入。报告逐例记录拦截阶段与问题
码；只有在期望阶段被拦下且至少带期望码时才算通过。这里不与任何机器人通信：被准入本身就是失败。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from drone_agent.admission.admission import AdmissionContext
from drone_agent.admission.airspace import SimulatedAirspaceProvider
from drone_agent.admission.compiler import CompileContext
from drone_agent.admission.models import MissionRequest, RequestChannel
from drone_agent.admission.pipeline import evaluate_mapping
from drone_agent.contracts import ControlMode, SkillRef, utcnow
from drone_agent.mission.registry import M2_SCENE, Registry
from drone_agent.runtime.permission import TrustLevel

CATEGORIES = ("wrong_coordinates", "capability", "parameters", "order", "prompt_injection", "scope_expansion")


def base_spec(case_id: str, now: datetime) -> dict:
    """The nominal single-asset inspection every deterministic case patches. / 所有确定性用例所叠加的标称单资产巡检。"""
    return {
        "mission_id": f"adv-{case_id}",
        "mission_version": 1,
        "goal": "Inspect the red equipment marker.",
        "goal_type": "inspect",
        "targets": [{"asset_id": "asset_red"}],
        "spatial_scope": {"approved_volume_id": "campus_training",
                          "frame": {"frame_id": "map_enu", "map_version": "campus_v2"}},
        "temporal_window": {"not_before": (now - timedelta(minutes=1)).isoformat(),
                            "not_after": (now + timedelta(minutes=30)).isoformat()},
        "tasks": [{"task_id": "inspect_asset_red", "skill_id": "skill.inspect.asset",
                   "params": {"asset_id": "asset_red"}}],
        "energy_budget": {"max_consumption_fraction": 0.78, "reserve_fraction": 0.2},
        "recovery_policy_ref": "multirotor_m1@v1",
        "provenance": {"model_id": "direct-construction", "prompt_version": "none", "input_hash": "0" * 64,
                       "generated_at": now.isoformat()},
    }


def case_inputs(case: dict, registry: Registry, now: datetime):
    """Spec mapping, compile context and admission context for one case. / 单个用例的规格映射与两个上下文。"""
    data = base_spec(case["id"], now)
    data.update(case.get("spec", {}))
    if "window" in case:
        start = now + timedelta(minutes=case["window"]["start_min"])
        data["temporal_window"] = {"not_before": start.isoformat(),
                                   "not_after": (start + timedelta(minutes=case["window"]["duration_min"])).isoformat()}
    request_values = {
        "request_id": f"adv-{case['id']}"[:80].replace(".", "-"),
        "text": data["goal"],
        "requested_by": "adversarial-corpus",
        "trust_level": TrustLevel.FIRST_PARTY,
        "channel": RequestChannel.HARNESS,
        "approved_volume_id": "campus_training",
        "idempotency_key": f"adv-{case['id']}",
        "received_at": now,
    }
    request_values.update(case.get("request", {}))
    request = MissionRequest(**request_values)
    context = case.get("context", {})
    capability = registry.capability.model_copy(deep=True)
    caps = context.get("capability", {})
    if "drop_skills" in caps:
        capability.skills = [s for s in capability.skills if s.skill_id not in set(caps["drop_skills"])]
    if "control_modes" in caps:
        capability.control_modes = {ControlMode(m) for m in caps["control_modes"]}
    if "skill_version" in caps:
        capability.skills = [SkillRef(skill_id=s.skill_id, version=caps["skill_version"]) for s in capability.skills]
    compile_ctx = CompileContext(registry=registry, robot_id=context.get("robot_id", capability.robot_id))
    admission_ctx = AdmissionContext(registry=registry, capability=capability, airspace=SimulatedAirspaceProvider(),
                                     now=now, request=request)
    return data, compile_ctx, admission_ctx


def run_case(case: dict, registry: Registry, now: datetime | None = None) -> dict:
    """Evaluate one deterministic case and judge it against its expectation. / 执行单个确定性用例并按期望判定。"""
    now = now or utcnow()
    data, compile_ctx, admission_ctx = case_inputs(case, registry, now)
    outcome = evaluate_mapping(data, compile_ctx, admission_ctx)
    expected = case["expected"]
    missing = sorted(set(expected["codes"]) - set(outcome.codes))
    reasons = []
    if outcome.blocked_at is None:
        reasons.append("not_blocked")
    elif outcome.blocked_at != expected["blocked_at"]:
        reasons.append(f"blocked_at {outcome.blocked_at} != {expected['blocked_at']}")
    if missing:
        reasons.append(f"missing codes {missing}")
    return {
        "id": case["id"],
        "category": case["category"],
        "blocked_at": outcome.blocked_at,
        "codes": outcome.codes,
        "expected": expected,
        "authorized_package": outcome.package is not None,
        "passed": not reasons,
        "reasons": reasons,
    }


def run_corpus(path: Path, root: Path) -> dict:
    """Run a deterministic corpus file and summarise it by category. / 运行一个确定性语料文件并按类别汇总。"""
    corpus = yaml.safe_load(path.read_text(encoding="utf-8"))
    if corpus.get("format") != "eval.adversarial/v1" or corpus.get("path") != "deterministic":
        raise ValueError("not a deterministic eval.adversarial/v1 corpus")
    registry = Registry(root, scene=root / M2_SCENE)
    now = utcnow()
    results = [run_case(case, registry, now) for case in corpus["cases"]]
    per_category = Counter(r["category"] for r in results)
    return {
        "schema_version": "0.1.0",
        "corpus": corpus["corpus"],
        "path": "deterministic",
        "cases": len(results),
        "blocked": sum(1 for r in results if r["blocked_at"] is not None),
        "passed": sum(1 for r in results if r["passed"]),
        "authorized_packages": sum(1 for r in results if r["authorized_package"]),
        "per_category": {c: per_category.get(c, 0) for c in CATEGORIES},
        "blocked_at": dict(Counter(r["blocked_at"] for r in results)),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_corpus(args.corpus, args.root)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] == report["cases"] else 1)


__all__ = ["CATEGORIES", "base_spec", "case_inputs", "replace", "run_case", "run_corpus"]

if __name__ == "__main__":
    main()
