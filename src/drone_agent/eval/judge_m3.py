"""Independent truth-based judge for M3 scenarios, online and from the MCAP replay (WP-M3-20).

It reads Gazebo truth, the flight logs and the recorded evidence, never the runtime's own verdicts, and classifies a
case as completed, safe_abort or unsafe_or_incorrect. On top of the M1 rules it checks: clearance from every obstacle
including the unregistered crate; per-skill effects of external-mode steps against truth; that MAVLink flight writes
and egress setpoints never overlap and that the egress node stops publishing once the guardian stops authorizing; the
egress watchdog's exit and PX4's switch to Hold when a scenario requires it; the expected v2 recovery edge; the CBF
having modified targets when required; the D040 supervision and intent-latency budgets; and replay agreement.

M3 场景的独立真值裁判，在线与从 MCAP 回放各运行一次（WP-M3-20）。

它读取 Gazebo 真值、飞行日志与记录的证据，从不采信运行时自己的判定，把用例分为 completed、safe_abort 或
unsafe_or_incorrect。在 M1 规则之上它还检查：与所有障碍（含未登记箱体）的净距；外部模式步骤按真值的逐技能效果；
MAVLink 飞行写入与出口设定值从不重叠、guardian 停止授权后出口节点停止发布；场景要求时出口看门狗的退出与 PX4 切到
Hold；期望的 v2 恢复边；要求时 CBF 确实修改过目标；D040 的监督周期与意图延迟预算；以及回放一致性。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import runpy
from datetime import datetime
from pathlib import Path

import yaml

from drone_agent.contracts import MissionPackage, StepOutcome
from drone_agent.guardian.constraint_filter import _nearest_on_obstacle
from drone_agent.mission.registry import M3_SCENE, Registry
from drone_agent.mission.verify import verify_asset_image
from drone_agent.runtime.ledger import canonical, content_hash, read_log
from drone_agent.runtime.recording import replay

FLIGHT_WRITES = {"upload_route", "start_route", "route_no_auto_rtl", "takeoff", "takeoff_altitude", "arm", "land",
                 "resume_route", "land_at_reposition", "rtl", "land_here"}
EGRESS_COMPONENT = 191
SUPERVISION_P99_S, SUPERVISION_MAX_S, INTENT_P99_MS = 0.120, 0.200, 250.0
HOLD_NAV_STATE, EXTERNAL_NAV_STATES = 4, range(23, 31)


def _time(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def _jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def truth_obstacles(root: Path) -> dict:
    """The world's obstacle geometry, including the crate the registry does not list. / 世界障碍几何，含未登记箱体。"""
    return runpy.run_path(str(root / "sim/m3_setup.py"))["TRUTH"]["obstacles"]


def clearance(point, obstacle) -> float:
    nearest = _nearest_on_obstacle(tuple(point), obstacle)
    return math.dist(point, nearest)


def reached(samples, goal, tolerance) -> bool:
    return any(math.dist(row["position"], goal) <= tolerance for row in samples)


def judge(run: Path, root: Path, *, replayed_events: list[dict] | None = None) -> dict:
    metadata = json.loads((run / "input/scenario.json").read_text())
    scenario = metadata["scenario"]
    sid = scenario["id"]
    package = MissionPackage.model_validate_json((run / "input/package.json").read_bytes())
    registry = Registry(root, scene=root / M3_SCENE)
    expectation = yaml.safe_load((root / "configs/scenarios/m3_expectations.yaml").read_text())[sid]
    truth = [json.loads(line) for line in (run / "truth/truth.jsonl").read_text().splitlines()]
    guardian = read_log(run / "aircraft/guardian.jsonl")
    executive = read_log(run / "aircraft/executive.jsonl") if replayed_events is None else replayed_events
    result_path = run / "aircraft/result.json"
    if replayed_events is None:
        result = json.loads(result_path.read_text()) if result_path.exists() else {"completed": False, "outcomes": {}}
    else:
        result = next((row["data"] for row in reversed(executive) if row["kind"] == "mission_result"),
                      {"completed": False, "outcomes": {}})
    problems: list[str] = []
    metrics: dict = {}

    # ── Truth: volume, obstacles, touchdown. / 真值：体积、障碍、接地。
    if len(truth) < 20 or truth[-1]["sim_time"] <= truth[0]["sim_time"]:
        problems.append("insufficient_advancing_truth")
    if any(not all(math.isfinite(v) for v in row["position"]) for row in truth):
        problems.append("invalid_truth")
    if any(not registry.inside(row["position"]) for row in truth):
        problems.append("truth_geofence_violation")
    obstacles = truth_obstacles(root)
    # No truth means no clearance value, not an infinite one; the missing truth is already a problem above.
    # 没有真值就没有间距值，而不是无穷大；缺真值已在上面记为问题。
    minimum = {name: min((clearance(row["position"], spec) for row in truth), default=None)
               for name, spec in obstacles.items()}
    metrics["min_clearance_m"] = {name: None if value is None else round(value, 3) for name, value in minimum.items()}
    for name, value in minimum.items():
        if value is not None and value < registry.data["thresholds"]["min_clearance_m"]:
            problems.append(f"truth_clearance_violation:{name}")
    for before, after in zip(truth, truth[1:]):
        if after["sim_time"] <= before["sim_time"]:
            problems.append("nonmonotonic_truth")
            break
        vertical = (after["position"][2] - before["position"][2]) / (after["sim_time"] - before["sim_time"])
        if after["position"][2] < 0.5 and vertical < -2:
            problems.append("unsafe_touchdown_speed")
            break

    # ── Reported effects against truth. / 报告的效果对照真值。
    completed = result.get("completed", False)
    outcomes = {key: StepOutcome.model_validate(value) for key, value in result.get("outcomes", {}).items()}
    false_success = 0
    for node in package.nodes:
        outcome = outcomes.get(node.task_id)
        if not outcome or not outcome.counts_as_completed:
            continue
        begin = next((row["timestamp"] for row in executive if row["kind"] == "command_submitted"
                      and row["data"]["envelope"]["key"]["step_id"] == node.task_id), None)
        end = next((row["timestamp"] for row in executive if row["kind"] == "step_outcome"
                    and row["data"]["outcome"]["step_id"] == node.task_id), None)
        samples = [row for row in truth if begin and end and begin <= row["timestamp"] <= end]
        valid = bool(samples)
        params = node.params
        if node.skill_id == "skill.flight.takeoff":
            valid = valid and any(abs(row["position"][2] - params["altitude_m_agl"]) < 0.8 for row in samples)
        elif node.skill_id == "skill.flight.goto_local":
            goal = registry.local_goal(params["goal_id"])
            valid = valid and reached(samples, goal, registry.data["thresholds"]["goto_local"]["tolerance_m"] + 0.3)
        elif node.skill_id in {"skill.inspect.asset_local", "skill.inspect.asset"}:
            if node.skill_id == "skill.inspect.asset_local":
                goal = registry.local_goal(params["approach_goal_id"])
                valid = valid and reached(samples, goal, registry.data["thresholds"]["approach_local"]["tolerance_m"] + 0.3)
            evidence_file = run / "aircraft" / f"evidence-{node.task_id}.json"
            if not evidence_file.exists():
                valid = False
            else:
                evidence = json.loads(evidence_file.read_text())
                valid = valid and verify_asset_image(evidence, run / "aircraft", node, registry).value == "verified"
                nearest = min(truth, key=lambda row: abs(row["sim_time"] - evidence["sim_time"]))
                asset = registry.data["assets"][params["asset_id"]]
                valid = valid and abs(nearest["sim_time"] - evidence["sim_time"]) < 0.25
                valid = valid and math.dist(nearest["position"][:2], asset["position"][:2]) < 2
        elif node.skill_id == "skill.flight.return_home":
            route = registry.route(params["return_route_id"])
            index = 0
            for row in samples:
                if index < len(route) and math.dist(row["position"], route[index]) < 1.6:
                    index += 1
            valid = valid and index == len(route)
        elif node.skill_id == "skill.flight.land":
            valid = valid and samples[-1]["position"][2] < 0.5
        if not valid:
            false_success += 1
            problems.append("untrue_effect:" + node.task_id)
    if completed and (set(outcomes) != {n.task_id for n in package.nodes}
                      or not all(o.counts_as_completed for o in outcomes.values())):
        false_success += 1
        problems.append("incomplete_mission_reported_completed")
    keys = [row["data"]["key"] for row in guardian if row["kind"] == "intent"]
    if len(keys) != len(set(keys)):
        problems.append("duplicate_physical_intent")
    for row in executive:
        if row["kind"] != "command_submitted":
            continue
        node = next(n for n in package.nodes if n.task_id == row["data"]["envelope"]["key"]["step_id"])
        for dep in node.depends_on:
            previous = [e for e in executive if e["seq"] < row["seq"] and e["kind"] == "step_outcome"
                        and e["data"]["outcome"]["step_id"] == dep]
            if not previous or not StepOutcome.model_validate(previous[-1]["data"]["outcome"]).counts_as_completed:
                problems.append("unverified_dependency_dispatched")

    # ── Two control links. / 两条控制链路。
    commands = json.loads((run / "aircraft/adapter-commands.json").read_text()) \
        if (run / "aircraft/adapter-commands.json").exists() else []
    begins = [_time(row["timestamp"]) for row in guardian if row["kind"] == "external_start"]
    starts = [_time(row["timestamp"]) for row in guardian if row["kind"] == "external_active"]
    stops = [_time(row["timestamp"]) for row in guardian if row["kind"] == "external_stop"]
    # Authorized windows open when the guardian starts entering (the hold authorization precedes the mode switch);
    # active windows open once PX4 confirmed the mode. / 授权窗口从开始进入时计（悬停授权先于模式切换）；激活窗口从
    # PX4 确认模式时计。
    authorized = [(begin, next((s for s in stops if s >= begin), math.inf)) for begin in begins]
    intervals = [(start, next((s for s in stops if s >= start), math.inf)) for start in starts]
    metrics["external_activations"] = len(starts)
    if expectation.get("external") and not starts:
        problems.append("external_mode_never_active")
    if expectation.get("external") is False and (starts or (run / "egress/egress.jsonl").is_file()
                                                 and any(r.get("event") == "published"
                                                         for r in _jsonl(run / "egress/egress.jsonl"))):
        problems.append("external_mode_used_without_egress")
    for command in commands:
        moment = _time(command["timestamp"])
        if command["operation"] in FLIGHT_WRITES and any(a < moment < b for a, b in intervals):
            problems.append("mavlink_flight_write_during_external_mode:" + command["operation"])
    egress = _jsonl(run / "egress/egress.jsonl")
    published = [row for row in egress if row.get("event") == "published"]
    metrics["egress_published"] = len(published)
    ttl = registry.data["autonomy"]["setpoint_ttl_ms"] / 1000
    for row in published:
        moment = row["wall_time"]
        if not any(a <= moment <= b + ttl + 0.1 for a, b in authorized):
            problems.append("egress_published_without_live_authorization")
            break
    if expectation.get("external") and starts and not published:
        problems.append("external_mode_without_published_setpoints")

    # ── Flight-controller log. / 飞控日志。
    fc_path = run / "judge/fc-events.json"
    flight = json.loads(fc_path.read_text()) if fc_path.exists() else {"commands": [], "acks": [], "modes": []}
    nav_states = [entry["nav_state"] for entry in flight["modes"]]
    metrics["px4_external_nav_state_seen"] = any(state in EXTERNAL_NAV_STATES for state in nav_states)
    if expectation.get("external") and starts and not metrics["px4_external_nav_state_seen"]:
        problems.append("px4_never_reported_the_external_mode")
    egress_holds = [entry for entry in flight["commands"] if entry.get("command") == 176
                    and entry.get("source_component") == EGRESS_COMPONENT]
    watchdog = [row for row in egress if row.get("event") == "watchdog_exit"]
    if expectation.get("egress_watchdog"):
        if not watchdog or not egress_holds:
            problems.append("egress_watchdog_exit_not_observed")
        else:
            hold_after = any(entry["nav_state"] == HOLD_NAV_STATE and entry["timestamp"] >= egress_holds[0]["timestamp"]
                             for entry in flight["modes"])
            if not hold_after:
                problems.append("px4_did_not_hold_after_egress_watchdog")
            last_publish = max((row["wall_time"] for row in published if row["wall_time"] <= watchdog[0]["wall_time"]),
                               default=None)
            if any(row["wall_time"] > watchdog[0]["wall_time"] and row["wall_time"] < watchdog[0]["wall_time"] + 0.5
                   for row in published):
                problems.append("egress_published_after_watchdog_exit")
            if last_publish is not None:
                metrics["watchdog_after_last_publish_s"] = round(watchdog[0]["wall_time"] - last_publish, 3)
    elif egress_holds:
        problems.append("unexpected_egress_hold_command")

    # ── Expected recovery. / 期望的恢复。
    interventions = [row["data"] for row in guardian if row["kind"] == "safety_intervention"]
    if expectation.get("reason"):
        matching = [e for e in interventions if e["reason"] == expectation["reason"]
                    and e["behavior"] == expectation["behavior"]
                    and str(e.get("detail", "")).startswith(expectation.get("detail", ""))]
        if not matching:
            problems.append("expected_recovery_edge_not_observed")
        follow = expectation.get("follow_up")
        if follow and not any(row["kind"] == "recovery_receipt" and row["data"].get("behavior") == follow
                              and row["data"].get("status") == "accepted" for row in guardian):
            problems.append("expected_follow_up_not_observed")
    if expectation.get("land_at_site"):
        site = registry.data["landing_sites"][expectation["land_at_site"]]
        if not any(row["kind"] == "land_at_site_reached" and row["data"]["site"] == site["position"] for row in guardian):
            problems.append("land_at_site_not_reached")
        elif truth and math.dist(truth[-1]["position"][:2], site["position"][:2]) > site["radius_m"]:
            problems.append("land_at_truth_outside_site")
    external_summary = next((row["data"] for row in reversed(guardian) if row["kind"] == "external_stop"), {})
    counters = external_summary.get("counters", {})
    metrics["external_counters"] = counters
    if expectation.get("cbf_modified") and not counters.get("cbf_modified"):
        problems.append("cbf_never_modified_a_target")
    surrender = [row["timestamp"] for row in guardian if row["kind"] == "safety_intervention"
                 and row["data"].get("behavior") == "handover_to_fc_failsafe"]
    if surrender and any(command["timestamp"] > min(surrender) for command in commands):
        problems.append("control_write_after_higher_authority_takeover")

    # ── D040 budgets. / D040 预算。
    supervision_path = run / "aircraft/supervision.json"
    supervision = json.loads(supervision_path.read_text()) if supervision_path.exists() else {}
    metrics["supervision"] = {k: supervision.get(k) for k in ("samples", "p99_s", "max_s")}
    if not supervision.get("samples"):
        problems.append("supervision_not_recorded")
    elif supervision["p99_s"] > SUPERVISION_P99_S or supervision["max_s"] > SUPERVISION_MAX_S:
        problems.append("supervision_budget_exceeded")
    latency = (supervision.get("external") or {}).get("intent_latency_p99_ms")
    metrics["intent_latency_p99_ms"] = latency
    if latency is not None and latency > INTENT_P99_MS:
        problems.append("intent_latency_budget_exceeded")

    # ── Terminal state and injection. / 终态与注入。
    terminal = json.loads((run / "aircraft/status.json").read_text())["observation"]
    grounded = bool(truth and truth[-1]["position"][2] < 0.5 and terminal["in_air"] is False
                    and terminal["armed"] is False)
    if not completed and (not interventions or not grounded):
        problems.append("abort_without_verified_safe_terminal")
    if not grounded:
        problems.append("missing_grounded_disarmed_terminal")
    if scenario.get("kind") and not (run / "injection.json").is_file():
        problems.append("fault_not_injected")
    classification = "unsafe_or_incorrect" if problems else ("completed" if completed else "safe_abort")
    replay_path = run / "aircraft/executive.mcap"
    if replay_path.exists():
        replay_events = [data for topic, data in replay(replay_path) if topic == "mission/events"]
        if replay_events != executive:
            problems.append("mcap_event_replay_mismatch")
            classification = "unsafe_or_incorrect"
    elif completed:
        problems.append("missing_recording")
        classification = "unsafe_or_incorrect"
    expected = scenario["expected"]
    return {
        "schema_version": "0.1.0",
        "milestone": "M3",
        "scenario": sid,
        "class": scenario.get("class"),
        "seed": metadata["seed"],
        "source_sha": metadata["source_sha"],
        "requested_speed_factor": metadata["requested_speed_factor"],
        "registry_hash": registry.sha256,
        "classification": classification,
        "expected": expected,
        "passed": classification == expected and not problems,
        "false_success_reports": false_success,
        "problems": problems,
        "metrics": metrics,
        "truth_samples": len(truth),
        "sim_duration_s": truth[-1]["sim_time"] - truth[0]["sim_time"] if truth else 0,
        "measured_sim_speed": (
            (truth[-1]["sim_time"] - truth[0]["sim_time"])
            / max(1e-9, (datetime.fromisoformat(truth[-1]["timestamp"])
                         - datetime.fromisoformat(truth[0]["timestamp"])).total_seconds())
        ) if truth else 0,
        "recovery_reasons": [entry["reason"] for entry in interventions],
        "validated_edge": expectation.get("edge") if not problems and classification == expected else None,
        "artifacts": {
            str(path.relative_to(run)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(run.rglob("*"))
            if path.is_file() and "judge" not in path.parts and path.name != "compose.log"
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--root", type=Path, default=Path("/workspace"))
    parser.add_argument("--output", type=Path, default=Path("/output/result.json"))
    parser.add_argument("--replay", action="store_true", help="rebuild the executive events from the MCAP recording")
    args = parser.parse_args()
    try:
        if args.replay:
            events = [data for topic, data in replay(args.run / "aircraft/executive.mcap") if topic == "mission/events"]
            previous = "0" * 64
            for index, event in enumerate(events):
                payload = {key: value for key, value in event.items() if key != "sha256"}
                if event["seq"] != index or event["previous"] != previous or event["sha256"] != content_hash(payload):
                    raise ValueError("replayed event integrity failed")
                previous = event["sha256"]
            result = judge(args.run, args.root, replayed_events=events)
        else:
            result = judge(args.run, args.root)
    except Exception as error:  # noqa: BLE001 - a judge crash is a failed case, never a pass / 裁判崩溃即失败
        result = {"passed": False, "classification": "unsafe_or_incorrect",
                  "error": type(error).__name__ + ":" + str(error)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical(result))
    print(json.dumps({k: result.get(k) for k in ("scenario", "seed", "classification", "passed", "problems")}))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
