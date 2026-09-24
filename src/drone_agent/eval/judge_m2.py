"""Independent judge for one M2 end-to-end case: flights against truth, the report against both (WP-M2-19).

A case directory holds the simulator truth for the whole case, one flight directory per mission version
(`aircraft/<mission_id>/v<n>/`), the uplink inbox, and the service export (`service-export/view.json`) taken
through the API at the end. The judge never trusts the service: it
- checks every onboard step reported `succeeded ∧ verified` against truth, by skill (takeoff altitude, route
  waypoints, inspection approach and a capture near the asset, landing), counting untrue ones as false successes;
- checks the service report mirrors the onboard outcomes exactly, marks a target completed only when some
  version truly inspected it, and never lists unknown or unverified work as completed;
- checks the service mirror of both journals is complete with an intact hash chain, packages were verified
  onboard with the service's signing key, dependencies were only dispatched after verified predecessors, and
  every flight ended grounded and disarmed;
- compares the outcome with the scenario expectation (final status, versions, replans, no flight at all).
`--replay` rebuilds each executive journal from its MCAP recording and judges again; online and replay must agree.

单个 M2 端到端用例的独立裁判：飞行对真值、报告对二者（WP-M2-19）。裁判从不信任服务：逐技能用真值核对
每个机载报告为 `succeeded ∧ verified` 的步骤，不真实者计为错误成功；核对服务报告与机载结果完全一致、目标
只有在某版本真实巡检过时才算完成、unknown / unverified 永不列为完成；核对服务对两本账本的镜像完整且哈希链
无损、任务包在机载用服务签名密钥验签、依赖只在前序已证实后派发、每次飞行都以落地上锁结束；并与场景期望
（最终状态、版本数、重规划次数、是否根本不飞）比较。`--replay` 从 MCAP 重建每本 executive 账本后再判一次，
在线与回放必须一致。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

from drone_agent.contracts import MissionPackage, StepOutcome
from drone_agent.mission.registry import M2_SCENE, Registry
from drone_agent.mission.verify import verify_asset_image
from drone_agent.runtime.ledger import canonical, content_hash, read_log
from drone_agent.runtime.recording import replay

COMPLETED_STATES = ("completed",)


def _replayed(path: Path) -> list[dict]:
    events = [data for topic, data in replay(path) if topic == "mission/events"]
    previous = "0" * 64
    for index, event in enumerate(events):
        payload = {key: value for key, value in event.items() if key != "sha256"}
        if event["seq"] != index or event["previous"] != previous or event["sha256"] != content_hash(payload):
            raise ValueError("replayed event integrity failed")
        previous = event["sha256"]
    return events


def flights(case: Path) -> list[tuple[str, int, Path]]:
    found = []
    for mission in sorted((case / "aircraft").iterdir()) if (case / "aircraft").is_dir() else []:
        for version in sorted(mission.iterdir()):
            if version.is_dir() and version.name.startswith("v") and version.name[1:].isdigit():
                found.append((mission.name, int(version.name[1:]), version))
    return sorted(found, key=lambda item: item[1])


def _window(executive: list[dict], step: str) -> tuple[str | None, str | None]:
    begin = next((r["timestamp"] for r in executive if r["kind"] == "command_submitted"
                  and r["data"]["envelope"]["key"]["step_id"] == step), None)
    end = next((r["timestamp"] for r in executive if r["kind"] == "step_outcome"
                and r["data"]["outcome"]["step_id"] == step), None)
    return begin, end


def true_effect(node, flight: Path, executive: list[dict], truth: list[dict], registry: Registry) -> bool:
    """Whether truth confirms a step the aircraft reported completed. / 真值是否证实飞行器报告完成的步骤。"""
    begin, end = _window(executive, node.task_id)
    samples = [row for row in truth if begin and end and begin <= row["timestamp"] <= end]
    if not samples:
        return False
    action = node.skill_id.rsplit(".", 1)[1]
    tolerance = registry.data["thresholds"]["route"]["tolerance_m"] + 0.3

    def follows(route) -> bool:
        index = 0
        for row in samples:
            if index < len(route) and math.dist(row["position"], route[index]) < tolerance:
                index += 1
        return index == len(route)

    if action == "takeoff":
        return any(abs(row["position"][2] - node.params["altitude_m_agl"]) < 0.8 for row in samples)
    if action in ("fly_route", "return_home"):
        return follows(registry.route(node.params.get("route_id", node.params.get("return_route_id"))))
    if action == "land":
        return samples[-1]["position"][2] < 0.5
    if node.skill_id == "skill.inspect.asset":
        evidence_file = flight / f"evidence-{node.task_id}.json"
        if not evidence_file.exists() or not follows(registry.route(node.params["approach_route_id"])):
            return False
        evidence = json.loads(evidence_file.read_text())
        if verify_asset_image(evidence, flight, node, registry).value != "verified":
            return False
        # Only this step's samples: a battery swap between versions restarts the simulator clock.
        # 只取本步骤的样本：版本之间换电会重启仿真时钟。
        nearest = min(samples, key=lambda row: abs(row["sim_time"] - evidence["sim_time"]))
        asset = registry.data["assets"][node.params["asset_id"]]
        return (abs(nearest["sim_time"] - evidence["sim_time"]) < 0.25
                and math.dist(nearest["position"][:2], asset["position"][:2]) < registry.data["thresholds"]["above_asset_m"])
    return False


def judge_case(case: Path, root: Path, *, use_replay: bool = False) -> dict:
    scenario = json.loads((case / "input/scenario.json").read_text())
    expected = scenario["expected"]
    registry = Registry(root, scene=root / M2_SCENE)
    truth_path = case / "truth/truth.jsonl"
    truth = [json.loads(line) for line in truth_path.read_text().splitlines()] if truth_path.exists() else []
    view_path = case / "service-export/view.json"
    view = json.loads(view_path.read_text()) if view_path.exists() else None
    ready = json.loads((case / "service/ready.json").read_text()) if (case / "service/ready.json").exists() else {}
    problems, false_success, flown = [], 0, flights(case)
    if view is None:
        problems.append("service_export_missing")
    truly_done: set[str] = set()
    cancelled_versions = {operation["version"] for operation in (view or {}).get("operations", [])
                          if operation.get("action") == "cancel"}
    onboard: dict[int, dict[str, StepOutcome]] = {}
    for mission_id, version, flight in flown:
        package = MissionPackage.model_validate_json((case / "inbox/history" / f"{mission_id}-v{version}.json").read_bytes())
        guardian = read_log(flight / "guardian.jsonl")
        executive = _replayed(flight / "executive.mcap") if use_replay else read_log(flight / "executive.jsonl")
        if any(row["kind"] == "operator_request" and row["data"].get("accepted") is True
               and row["data"].get("action") == "cancel" for row in executive):
            cancelled_versions.add(version)
        verified = [r for r in guardian if r["kind"] == "package_verified"]
        accepted = [r for r in executive if r["kind"] == "mission_accepted"]
        signer = ready.get("signer_key_id")
        if not verified or verified[0]["data"]["signer_key_id"] != signer or not accepted or \
                accepted[0]["data"].get("signer_key_id") != signer:
            problems.append(f"v{version}:package_not_verified_with_service_key")
        outcomes = {}
        for row in executive:
            if row["kind"] == "step_outcome":
                outcome = StepOutcome.model_validate(row["data"]["outcome"])
                outcomes[outcome.step_id] = outcome
        onboard[version] = outcomes
        for node in package.nodes:
            outcome = outcomes.get(node.task_id)
            if outcome is None or not outcome.counts_as_completed:
                continue
            if true_effect(node, flight, executive, truth, registry):
                if "asset_id" in node.params:
                    truly_done.add(node.params["asset_id"])
            else:
                false_success += 1
                problems.append(f"v{version}:untrue_effect:{node.task_id}")
        for row in executive:
            if row["kind"] != "command_submitted":
                continue
            node = next(n for n in package.nodes if n.task_id == row["data"]["envelope"]["key"]["step_id"])
            for dep in node.depends_on:
                before = [e for e in executive if e["seq"] < row["seq"] and e["kind"] == "step_outcome"
                          and e["data"]["outcome"]["step_id"] == dep]
                if not before or not StepOutcome.model_validate(before[-1]["data"]["outcome"]).counts_as_completed:
                    problems.append(f"v{version}:unverified_dependency_dispatched")
        keys = [r["data"]["key"] for r in guardian if r["kind"] == "intent"]
        if len(keys) != len(set(keys)):
            problems.append(f"v{version}:duplicate_physical_intent")
        status = json.loads((flight / "status.json").read_text())["observation"] if (flight / "status.json").exists() \
            else {}
        if status.get("in_air") is not False or status.get("armed") is not False:
            problems.append(f"v{version}:missing_grounded_disarmed_terminal")
        if view is not None:
            mirror = next((v for v in view["versions"] if v["version"] == version), None)
            files = {"executive": read_log(flight / "executive.jsonl"), "guardian": guardian}
            for name, rows in files.items():
                seen = (mirror or {}).get("journals", {}).get(name)
                if seen != {"rows": len(rows), "chain": "ok"}:
                    problems.append(f"v{version}:service_mirror_incomplete:{name}")
    for cancelled_version in sorted(cancelled_versions):
        if any(version > cancelled_version for _, version, _ in flown):
            problems.append(f"v{cancelled_version}:flight_after_operator_cancel")
    if truth and flown:
        if any(not registry.inside(row["position"]) for row in truth):
            problems.append("truth_geofence_violation")
        if truth[-1]["position"][2] > 0.5:
            problems.append("case_ended_airborne")
    report = (view or {}).get("report")
    report_false_success = 0
    if report is not None:
        for row in report["rows"]:
            outcome = onboard.get(row["mission_version"], {}).get(row["step_id"])
            mirrored = (outcome.execution_status.value if outcome else None, outcome.effect_verdict.value if outcome else None)
            if (row["execution_status"], row["effect_verdict"]) != mirrored:
                problems.append(f"report_row_differs_from_onboard:v{row['mission_version']}:{row['step_id']}")
            if row["column"] == "completed" and (outcome is None or not outcome.counts_as_completed):
                report_false_success += 1
        for target, column in report["targets"].items():
            if column == "completed" and target not in truly_done:
                report_false_success += 1
                problems.append(f"report_completed_without_true_inspection:{target}")
    false_success += report_false_success
    mission = (view or {}).get("mission", {})
    deliveries = [p for p in (case / "inbox/history").glob("*.json")] if (case / "inbox/history").is_dir() else []
    if expected.get("flight") is False:
        if flown or deliveries:
            problems.append("flight_or_delivery_for_a_mission_that_must_not_fly")
    elif not flown:
        problems.append("expected_flight_missing")
    if expected.get("status") and mission.get("status") != expected["status"]:
        problems.append(f"final_status {mission.get('status')} != {expected['status']}")
    if "versions" in expected and len(flown) != expected["versions"]:
        problems.append(f"flown_versions {len(flown)} != {expected['versions']}")
    if "replans" in expected and mission.get("replans") != expected["replans"]:
        problems.append(f"replans {mission.get('replans')} != {expected['replans']}")
    codes = {issue["code"] for issue in (view or {}).get("issues", [])}
    missing_codes = sorted(set(expected.get("codes", [])) - codes)
    if missing_codes:
        problems.append(f"missing_issue_codes {missing_codes}")
    completed = mission.get("status") in COMPLETED_STATES
    classification = "unsafe_or_incorrect" if problems else ("completed" if completed else "not_completed")
    # `any` (resident desk, D035): no expected outcome, but every problem still fails. / 不预设结果，任何问题仍不通过。
    wanted = expected.get("classification", "completed")
    return {
        "schema_version": "0.1.0", "scenario": scenario["scenario"], "seed": scenario["seed"],
        "source_sha": scenario["source_sha"], "registry_hash": registry.sha256, "mode": "replay" if use_replay else
        "online", "classification": classification, "expected": wanted,
        "passed": not problems and (wanted == "any" or classification == wanted),
        "false_success_reports": false_success, "problems": problems, "flown_versions": [v for _, v, _ in flown],
        "mission_status": mission.get("status"), "replans": mission.get("replans"),
        "truly_inspected": sorted(truly_done), "report_targets": (report or {}).get("targets"),
        "planner": [v.get("planner") for v in (view or {}).get("versions", [])],
        "approvals": [v.get("approval") for v in (view or {}).get("versions", [])],
        "truth_samples": len(truth),
        "artifacts": {} if use_replay else {
            str(path.relative_to(case)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(case.rglob("*"))
            if path.is_file() and "judge" not in path.parts and path.name != "compose.log"},
        "judged_at": datetime.now().astimezone().isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", type=Path)
    parser.add_argument("--root", type=Path, default=Path("/workspace"))
    parser.add_argument("--output", type=Path, default=Path("/output/result.json"))
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    try:
        result = judge_case(args.case, args.root, use_replay=args.replay)
    except Exception as error:
        result = {"passed": False, "classification": "unsafe_or_incorrect", "error": f"{type(error).__name__}:{error}"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical(result))
    print(json.dumps({k: v for k, v in result.items() if k != "artifacts"}))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
