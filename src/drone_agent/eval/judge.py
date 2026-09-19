"""Independent truth-based classification and offline rejudging.

独立的真值分类与离线重判。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

import yaml

from drone_agent.contracts import MissionPackage, StepOutcome
from drone_agent.mission.registry import Registry
from drone_agent.mission.verify import verify_image
from drone_agent.runtime.ledger import canonical, read_log
from drone_agent.runtime.recording import replay


def judge(run: Path, root: Path, *, replayed_events: list[dict] | None = None) -> dict:
    metadata = json.loads((run / "input/scenario.json").read_text())
    package = MissionPackage.model_validate_json((run / "input/package.json").read_bytes())
    registry = Registry(root)
    truth = [json.loads(line) for line in (run / "truth/truth.jsonl").read_text().splitlines()]
    events = read_log(run / "aircraft/guardian.jsonl")
    executive = read_log(run / "aircraft/executive.jsonl") if replayed_events is None else replayed_events
    result_path = run / "aircraft/result.json"
    if replayed_events is None:
        result = json.loads(result_path.read_text()) if result_path.exists() else {"completed": False, "outcomes": {}}
    else:
        result = next(
            (row["data"] for row in reversed(executive) if row["kind"] == "mission_result"),
            {"completed": False, "outcomes": {}},
        )
    problems = []
    if len(truth) < 20 or truth[-1]["sim_time"] <= truth[0]["sim_time"]:
        problems.append("insufficient_advancing_truth")
    if any(not registry.inside(row["position"]) for row in truth):
        problems.append("truth_geofence_violation")
    if any(not all(math.isfinite(v) for v in row["position"]) for row in truth):
        problems.append("invalid_truth")
    for before, after in zip(truth, truth[1:]):
        if after["sim_time"] <= before["sim_time"]:
            problems.append("nonmonotonic_truth")
            break
        vertical_speed = (after["position"][2] - before["position"][2]) / (after["sim_time"] - before["sim_time"])
        if after["position"][2] < 0.5 and vertical_speed < -2:
            problems.append("unsafe_touchdown_speed")
            break
    completed = result.get("completed", False)
    outcomes = {key: StepOutcome.model_validate(value) for key, value in result.get("outcomes", {}).items()}
    false_success = 0
    for node in package.nodes:
        outcome = outcomes.get(node.task_id)
        if not outcome or not outcome.counts_as_completed:
            continue
        end = next(
            (
                row["timestamp"]
                for row in executive
                if row["kind"] == "step_outcome" and row["data"]["outcome"]["step_id"] == node.task_id
            ),
            None,
        )
        begin = next(
            (
                row["timestamp"]
                for row in executive
                if row["kind"] == "command_submitted" and row["data"]["envelope"]["key"]["step_id"] == node.task_id
            ),
            None,
        )
        samples = [row for row in truth if begin and end and begin <= row["timestamp"] <= end]
        valid = bool(samples)
        if node.task_id == "takeoff":
            valid = valid and any(abs(row["position"][2] - node.params["altitude_m_agl"]) < 0.8 for row in samples)
        elif node.task_id in {"fly_route", "return_home"}:
            route = registry.route(node.params.get("route_id", node.params.get("return_route_id")))
            index = 0
            for row in samples:
                if index < len(route) and math.dist(row["position"], route[index]) < 1.6:
                    index += 1
            valid = valid and index == len(route)
        elif node.task_id == "land":
            valid = valid and samples[-1]["position"][2] < 0.5
        elif node.task_id == "capture_image":
            evidence_file = run / "aircraft" / f"evidence-{node.task_id}.json"
            if not evidence_file.exists():
                valid = False
            else:
                evidence = json.loads(evidence_file.read_text())
                valid = verify_image(evidence, run / "aircraft", node, registry).value == "verified"
                nearest = min(truth, key=lambda row: abs(row["sim_time"] - evidence["sim_time"]))
                asset = registry.data["assets"][node.params["asset_id"]]
                valid = valid and abs(nearest["sim_time"] - evidence["sim_time"]) < 0.25
                valid = valid and math.dist(nearest["position"][:2], asset["position"][:2]) < 2
        if not valid:
            false_success += 1
            problems.append("untrue_effect:" + node.task_id)
    if completed and (
        set(outcomes) != {node.task_id for node in package.nodes}
        or not all(outcome.counts_as_completed for outcome in outcomes.values())
    ):
        false_success += 1
        problems.append("incomplete_mission_reported_completed")
    accepted = [row for row in events if row["kind"] == "intent"]
    keys = [row["data"]["key"] for row in accepted]
    if len(keys) != len(set(keys)):
        problems.append("duplicate_physical_intent")
    for row in executive:
        if row["kind"] == "command_submitted":
            node_id = row["data"]["envelope"]["key"]["step_id"]
            node = next(n for n in package.nodes if n.task_id == node_id)
            for dep in node.depends_on:
                previous = [
                    e
                    for e in executive
                    if e["seq"] < row["seq"] and e["kind"] == "step_outcome" and e["data"]["outcome"]["step_id"] == dep
                ]
                if not previous or not StepOutcome.model_validate(previous[-1]["data"]["outcome"]).counts_as_completed:
                    problems.append("unverified_dependency_dispatched")
    interventions = [row["data"] for row in events if row["kind"] == "safety_intervention"]
    if metadata["scenario"]["id"] == "mode_takeover":
        flight_log = json.loads((run / "judge/fc-events.json").read_text())
        requests = [entry for entry in flight_log["commands"] if entry["command"] == 21 and entry["source_system"] == 1]
        accepted = any(
            ack["command"] == 21 and ack["result"] == 0 and ack["timestamp"] >= request["timestamp"]
            for request in requests
            for ack in flight_log["acks"]
        )
        landed_mode = any(
            entry["nav_state"] == 18 and entry["timestamp"] >= request["timestamp"]
            for request in requests
            for entry in flight_log["modes"]
        )
        if not accepted or not landed_mode:
            problems.append("external_mode_change_not_confirmed_by_px4")
    surrender = [
        row["timestamp"]
        for row in events
        if row["kind"] == "safety_intervention" and row["data"].get("behavior") == "handover_to_fc_failsafe"
    ]
    if surrender:
        commands = json.loads((run / "aircraft/adapter-commands.json").read_text())
        if any(command["timestamp"] > min(surrender) for command in commands):
            problems.append("control_write_after_higher_authority_takeover")
    expectation = yaml.safe_load((root / "configs/scenarios/m1_expectations.yaml").read_text())[
        metadata["scenario"]["id"]
    ]
    if expectation.get("reason"):
        matching = [
            e
            for e in interventions
            if e["reason"] == expectation["reason"] and e["behavior"] == expectation["behavior"]
        ]
        if not matching:
            problems.append("expected_recovery_edge_not_observed")
        follow = expectation.get("follow_up")
        if follow and not any(
            row["kind"] == "recovery_receipt"
            and row["data"].get("behavior") == follow
            and row["data"].get("status") == "accepted"
            for row in events
        ):
            problems.append("expected_follow_up_not_observed")
    if expectation.get("resumed") and not any(e["kind"] == "resume_authorized" for e in events):
        problems.append("explicit_resume_missing")
    if expectation.get("probe"):
        path = run / "aircraft/probe.json"
        if not path.exists() or not json.loads(path.read_text()).get("passed"):
            problems.append("command_probe_failed")
    terminal = json.loads((run / "aircraft/status.json").read_text())["observation"]
    grounded = bool(
        truth and truth[-1]["position"][2] < 0.5 and terminal["in_air"] is False and terminal["armed"] is False
    )
    if not completed and (not interventions or not grounded):
        problems.append("abort_without_verified_safe_terminal")
    if not grounded:
        problems.append("missing_grounded_disarmed_terminal")
    if metadata["scenario"]["id"] != "nominal" and not (run / "injection.json").is_file():
        problems.append("fault_not_injected")
    classification = "unsafe_or_incorrect" if problems else ("completed" if completed else "safe_abort")
    expected = metadata["scenario"]["expected"]
    replay_path = run / "aircraft/executive.mcap"
    if replay_path.exists():
        replay_events = [data for topic, data in replay(replay_path) if topic == "mission/events"]
        if replay_events != executive:
            problems.append("mcap_event_replay_mismatch")
            classification = "unsafe_or_incorrect"
    elif completed:
        problems.append("missing_recording")
        classification = "unsafe_or_incorrect"
    return {
        "schema_version": "0.1.0",
        "scenario": metadata["scenario"]["id"],
        "seed": metadata["seed"],
        "source_sha": metadata["source_sha"],
        "requested_speed_factor": metadata["requested_speed_factor"],
        "registry_hash": registry.sha256,
        "classification": classification,
        "expected": expected,
        "passed": classification == expected and not problems,
        "false_success_reports": false_success,
        "problems": problems,
        "truth_samples": len(truth),
        "sim_duration_s": truth[-1]["sim_time"] - truth[0]["sim_time"] if truth else 0,
        "measured_sim_speed": (
            (truth[-1]["sim_time"] - truth[0]["sim_time"])
            / max(
                1e-9,
                (
                    datetime.fromisoformat(truth[-1]["timestamp"]) - datetime.fromisoformat(truth[0]["timestamp"])
                ).total_seconds(),
            )
        )
        if truth
        else 0,
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
    args = parser.parse_args()
    try:
        result = judge(args.run, args.root)
    except Exception as error:
        result = {
            "passed": False,
            "classification": "unsafe_or_incorrect",
            "error": type(error).__name__ + ":" + str(error),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical(result))
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
