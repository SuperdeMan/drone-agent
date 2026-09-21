"""The evidence viewer is a read-only lens over recorded artifacts: it shows and verifies, never decides.

证据浏览器是记录产物的只读透镜：只展示、只核对，从不判定。
"""

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import yaml

from drone_agent.contracts import FlightObservation, Frame, MissionPackage, Pose, Position, utcnow
from drone_agent.eval import viewer
from drone_agent.mission.registry import Registry
from drone_agent.runtime.ledger import Journal
from drone_agent.runtime.recording import Recorder

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = yaml.safe_load((ROOT / "configs/platforms/px4_sitl_multirotor.yaml").read_text(encoding="utf-8"))
COVARIANCE = (0.81, 0.0, 0.0, 0.0, 0.81, 0.0, 0.0, 0.0, 3.1)


def digest_map(case: Path) -> dict:
    """The judge's coverage rule, recomputed independently of the viewer. / 独立于浏览器重算的裁判覆盖规则。"""
    return {
        p.relative_to(case).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(case.rglob("*"))
        if p.is_file() and "judge" not in p.parts
    }


def make_case(base: Path, name: str = "pause_resume-7", *, note: str = "") -> Path:
    """A synthetic but complete run: journals, MCAP, truth, evidence, judge. / 合成但完整的一次运行。"""
    case = base / name
    for folder in ("input", "aircraft/images", "truth", "judge"):
        (case / folder).mkdir(parents=True)
    registry = Registry(ROOT)
    start = utcnow()
    mission = f"m1-{name}-test"
    scenario = {"scenario": {"id": name.rsplit("-", 1)[0], "expected": "completed"}, "seed": 7, "source_sha": "a" * 40,
                "requested_speed_factor": 1, "registry_hash": registry.sha256, "route_speed_mps": 1.8, "note": note}
    (case / "input/scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
    motion = [{"resource_id": "uav_01.motion", "mode": "exclusive"}]
    package = MissionPackage.model_validate({
        "mission_id": mission, "mission_version": 1, "recovery_policy_ref": "multirotor_m1@v1", "nodes": [
            {"task_id": "takeoff", "skill_id": "skill.flight.takeoff", "skill_version": "0.1.0", "robot_id": "uav_01",
             "params": {"altitude_m_agl": 4}, "resources": motion, "timeout_s": 60},
            {"task_id": "fly_route", "skill_id": "skill.flight.fly_route", "skill_version": "0.1.0", "robot_id": "uav_01",
             "params": {"route_id": "inspection", "frame_id": "map_enu", "map_version": "campus_v1", "speed_mps": 1.8},
             "depends_on": ["takeoff"], "resources": motion, "timeout_s": 600},
            {"task_id": "capture_image", "skill_id": "skill.flight.capture_image", "skill_version": "0.1.0",
             "robot_id": "uav_01", "params": {"asset_id": "asset_red", "camera_id": "cam_0"}, "depends_on": ["fly_route"],
             "resources": [{"resource_id": "uav_01.camera", "mode": "exclusive"}], "timeout_s": 60},
            {"task_id": "land", "skill_id": "skill.flight.land", "skill_version": "0.1.0", "robot_id": "uav_01",
             "params": {"landing_site_id": "home_pad"}, "depends_on": ["capture_image"], "resources": motion, "timeout_s": 120},
        ],
    })
    (case / "input/package.json").write_text(package.model_dump_json(), encoding="utf-8")

    truth_rows = []
    for i in range(30):
        z = min(i, 4) if i < 20 else max(0.0, 4 - (i - 20) * 0.5)
        truth_rows.append({"timestamp": (start + timedelta(seconds=i)).isoformat(), "sim_time": 3.0 + i,
                           "position": [min(i * 0.2, 4.0), min(i * 0.2, 4.0), z], "orientation": [0, 0, 0, 1]})
    (case / "truth/truth.jsonl").write_text("\n".join(json.dumps(r) for r in truth_rows) + "\n", encoding="utf-8")

    executive, events = Journal(case / "aircraft/executive.jsonl"), Recorder(case / "aircraft/executive.mcap")

    def record(kind, **data):
        events.write("mission/events", executive.append(kind, {"mission_id": mission, **data}))

    def envelope(step, seq):
        return {"envelope": {"command_seq": seq, "intent_kind": "skill", "issued_at": start.isoformat(), "key": {
            "command_id": f"c{seq}", "lease_epoch": 1, "mission_id": mission, "mission_version": 1, "robot_id": "uav_01",
            "step_id": step}}}

    def outcome(step, status="succeeded", effect="verified", safety="proceed"):
        return {"mission_id": mission, "mission_version": 1, "step_id": step, "robot_id": "uav_01",
                "execution_status": status, "effect_verdict": effect, "safety_verdict": safety}

    record("mission_accepted", package_hash="f" * 64, registry_hash=registry.sha256)
    for seq, step in enumerate(["takeoff", "fly_route", "capture_image", "land"]):
        record("skill_state", step_id=step, previous="accepted", state="preparing")
        record("command_submitted", **envelope(step, seq))
        record("skill_state", step_id=step, previous="preparing", state="running")
        if step == "fly_route":
            record("operator_request", action="pause", request_id="pause_resume")
            record("operator_request", action="resume", request_id="resume")
        record("step_outcome", outcome=outcome(step))
    record("mission_result", completed=True, not_run=[], outcomes={})
    executive.close()
    events.close()

    guardian = Journal(case / "aircraft/guardian.jsonl")
    for kind, data in [
        ("lease", {"lease_epoch": 1, "holder": "executive:" + mission, "resources": ["uav_01.camera", "uav_01.motion"]}),
        ("intent", {**envelope("takeoff", 0), "key": "k0", "digest": "0" * 64}),
        ("receipt", {"key": "k0", "receipt": "accepted", "reason": ""}),
        ("fault_injected", {"injection": {"id": "pause_resume", "kind": "pause_resume"}, "boundary": "runtime_input"}),
        ("safety_intervention", {"reason": "user_pause", "behavior": "hold", "epoch": 1, "edge_hash": "e" * 64}),
        ("recovery_receipt", {"behavior": "hold", "status": "accepted"}),
        ("resume_authorized", {"request_id": "resume"}),
        ("recovered_to", {"state": "landed_disarmed"}),
    ]:
        guardian.append(kind, {"mission_id": mission, **data})
    guardian.close()

    observations = Recorder(case / "aircraft/guardian.mcap")
    for i in range(0, 30, 2):
        when = start + timedelta(seconds=i)
        z = min(i, 4) if i < 20 else max(0.0, 4 - (i - 20) * 0.5)
        value = FlightObservation(
            timestamp=when, valid_until=when + timedelta(seconds=0.5), sample_id=i, robot_id="uav_01",
            pose=Pose(frame=Frame(frame_id="map_enu", map_version="campus_v1"),
                      position=Position(x=min(i * 0.2, 4.0) + 0.1, y=min(i * 0.2, 4.0), z=z, covariance=COVARIANCE)),
            armed=True, in_air=i > 1, flight_mode="MISSION", localization_healthy=True, battery_fraction=0.9,
        ).model_dump(mode="json")
        # The guardian records every tick, so identical samples repeat; the viewer must deduplicate.
        # guardian 每个周期都记录，相同样本会重复；浏览器必须去重。
        observations.write("flight/observation", value)
        observations.write("flight/observation", value)
    observations.close()

    raw = bytes([200, 30, 30] * 12)
    sha = hashlib.sha256(raw).hexdigest()
    (case / f"aircraft/images/{sha}.rgb").write_bytes(raw)
    evidence = {"asset_id": "asset_red", "capture_timestamp": (start + timedelta(seconds=15)).isoformat(), "sim_time": 18.0,
                "width": 4, "height": 3, "media_ref": f"images/{sha}.rgb", "sha256": sha, "skill_instance": "capture_image",
                "source": "gazebo_rgb", "contract": {"captured_pose": {"position": {"x": 3.9, "y": 4.0, "z": 4.0,
                                                                                    "covariance": list(COVARIANCE)}},
                                                     "quality": {"width": 4.0, "height": 3.0}}}
    (case / "aircraft/evidence-capture_image.json").write_text(json.dumps(evidence), encoding="utf-8")
    (case / "aircraft/status.json").write_text(json.dumps({
        "observation": {"in_air": False, "armed": False, "flight_mode": "MISSION", "battery_fraction": 0.88},
        "phase": "ground", "safety": "hold", "reason": "mission_complete"}), encoding="utf-8")
    (case / "aircraft/supervision.json").write_text(json.dumps({"samples": 300, "p99_s": 0.1017, "max_s": 0.112}))
    (case / "aircraft/capabilities.json").write_text(json.dumps(PLATFORM["capability"]), encoding="utf-8")
    for role in ("executive", "guardian"):
        (case / f"aircraft/{role}-identity.json").write_text(json.dumps(
            {"pid": 1, "role": role, "mission_id": mission, "source_sha": "a" * 40, "registry_hash": registry.sha256}))
    (case / "injection.json").write_text(json.dumps(
        {"id": "pause_resume", "kind": "pause_resume", "timestamp": (start + timedelta(seconds=8)).isoformat(), "delay_s": 0.2}))
    (case / "aircraft/adapter-commands.json").write_text(json.dumps(
        [{"timestamp": (start + timedelta(seconds=1)).isoformat(), "kind": "arm_takeoff", "altitude": 4}]))
    (case / "judge/fc-events.json").write_text(json.dumps({"commands": [], "acks": [{"timestamp": 5_000_000, "command": 22, "result": 0}],
                                                           "modes": [{"timestamp": 1_000_000, "nav_state": 4, "arming_state": 1},
                                                                     {"timestamp": 3_000_000, "nav_state": 17, "arming_state": 2},
                                                                     {"timestamp": 3_100_000, "nav_state": 17, "arming_state": 2},
                                                                     {"timestamp": 9_000_000, "nav_state": 3, "arming_state": 2}]}))
    result = {"schema_version": "0.1.0", "scenario": scenario["scenario"]["id"], "seed": 7, "source_sha": "a" * 40,
              "requested_speed_factor": 1, "registry_hash": registry.sha256, "classification": "completed",
              "expected": "completed", "passed": True, "false_success_reports": 0, "problems": [], "truth_samples": 30,
              "sim_duration_s": 29.0, "measured_sim_speed": 0.95, "recovery_reasons": ["user_pause"], "validated_edge": None,
              "artifacts": digest_map(case)}
    (case / "judge/result.json").write_text(json.dumps(result), encoding="utf-8")
    (case / "judge/replay.json").write_text(json.dumps({k: v for k, v in result.items() if k != "artifacts"}))
    return case


def page_data(path: Path) -> dict:
    html = path.read_text(encoding="utf-8")
    marker = '<script id="record-data" type="application/json">'
    begin = html.index(marker) + len(marker)
    return json.loads(html[begin:html.index("</script>", begin)].replace("<\\/", "</"))


def test_page_reflects_the_recorded_run(tmp_path):
    case = make_case(tmp_path)
    page = viewer.build_page([case], root=ROOT, output=tmp_path / "page.html")
    record = page_data(page)["cases"][0]
    series = {s["id"]: s for s in record["series"]}
    assert set(series) == {"truth", "estimate"}
    assert len(series["truth"]["points"]) == 30 and len(series["estimate"]["points"]) == 15
    assert series["estimate"]["points"][0][4] == 0.9  # sigma_xy from the 0.81 covariance / 由 0.81 协方差得到的 σxy
    lanes = {e["lane"] for e in record["events"]}
    assert {"mission", "authority", "safety", "injection", "operator"} <= lanes
    assert any(e["kind"] == "injection.json" and e["lane"] == "injection" for e in record["events"])
    assert [s["id"] for s in record["steps"]] == ["takeoff", "fly_route", "capture_image", "land"]
    takeoff = record["steps"][0]
    assert takeoff["outcome"]["effect_verdict"] == "verified" and takeoff["start"] is not None and takeoff["end"] >= takeoff["start"]
    assert takeoff["states"][-1][2] == "running"
    (photo,) = record["gallery"]
    assert photo["png"].startswith("data:image/png;base64,") and photo["sha_ok"] is True
    assert photo["effect_verdict"] == "verified" and isinstance(photo["truth_distance_to_asset_m"], float)
    assert record["verdict"]["classification"] == "completed" and record["verdict"]["replay"]["agrees"] is True
    integrity = record["integrity"]
    assert integrity["journals"] == {"executive": "ok", "guardian": "ok"}
    assert integrity["mcap_replay_matches_journal"] is True
    assert integrity["judge_digests"]["matched"] == integrity["judge_digests"]["checked"] > 0
    assert integrity["judge_digests"]["mismatched"] == [] and integrity["judge_digests"]["missing"] == []
    assert record["world"]["present"] and record["world"]["matches_run"] is True
    tables = {t["id"]: t for t in record["tables"]}
    assert [row[1:] for row in tables["fc_modes"]["rows"]] == [["AUTO_LOITER", "DISARMED"], ["AUTO_TAKEOFF", "ARMED"], ["AUTO_MISSION", "ARMED"]]
    assert "harness" in tables and "adapter" in tables
    assert record["supervision"]["budget_s"] == 0.1 and record["terminal"]["armed"] is False
    assert record["identity"]["platform"]["robot_id"] == "uav_01" and record["identity"]["control_modes"] == ["mission_upload"]


def test_tampering_is_flagged_and_the_verdict_is_left_alone(tmp_path):
    case = make_case(tmp_path)
    with (case / "truth/truth.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"timestamp": utcnow().isoformat(), "sim_time": 99.0, "position": [0, 0, 0]}) + "\n")
    guardian = case / "aircraft/guardian.jsonl"
    body = bytearray(guardian.read_bytes())
    body[len(body) // 2] ^= 0x01
    guardian.write_bytes(bytes(body))
    record = page_data(viewer.build_page([case], root=ROOT, output=tmp_path / "page.html"))["cases"][0]
    assert record["integrity"]["judge_digests"]["mismatched"] == ["aircraft/guardian.jsonl", "truth/truth.jsonl"]
    assert record["integrity"]["journals"]["guardian"] != "ok" and record["integrity"]["journals"]["executive"] == "ok"
    # The page reports the judge's classification verbatim; it never upgrades or downgrades it.
    # 页面原样呈现裁判分类；既不升级也不降级。
    assert record["verdict"]["classification"] == "completed"


def test_missing_artifacts_are_reported_not_invented(tmp_path):
    case = tmp_path / "nominal-19"
    (case / "input").mkdir(parents=True)
    (case / "input/scenario.json").write_text(json.dumps({"scenario": {"id": "nominal", "expected": "completed"}, "seed": 19}))
    page = viewer.build_page([case], root=ROOT, output=tmp_path / "page.html")
    record = page_data(page)["cases"][0]
    assert record["verdict"] == {"judged": False}
    assert record["series"] == [] and record["steps"] == [] and record["gallery"] == []
    assert {"judge/result.json", "truth/truth.jsonl", "aircraft/executive.jsonl"} <= set(record["absent"])
    assert record["integrity"]["judge_digests"]["present"] is False
    assert "未裁判" in page.read_text(encoding="utf-8")


def test_batch_discovery_cross_checks_receipt_digests(tmp_path):
    good, bad = make_case(tmp_path, "nominal-7"), make_case(tmp_path, "nominal-19")
    rows = []
    for case in (good, bad):
        artifacts = digest_map(case)
        if case is bad:
            artifacts["truth/truth.jsonl"] = "0" * 64
        rows.append({"scenario": "nominal", "seed": int(case.name.split("-")[1]), "passed": True, "classification": "completed",
                     "replay_agrees": True, "judge_exit_code": 0, "artifacts": artifacts})
    (tmp_path / "receipt.json").write_text(json.dumps({"status": "passed", "source_sha": "a" * 40, "deployment_id": "d", "results": rows}))
    assert viewer.main([str(tmp_path), "--root", str(ROOT)]) == 0
    data = page_data(tmp_path / "viewer.html")
    assert data["receipt"]["source_sha"] == "a" * 40 and [c["id"] for c in data["cases"]] == ["nominal-19", "nominal-7"]
    by_id = {c["id"]: c for c in data["cases"]}
    assert by_id["nominal-7"]["integrity"]["receipt_digests"]["mismatched"] == []
    assert by_id["nominal-19"]["integrity"]["receipt_digests"]["mismatched"] == ["truth/truth.jsonl"]
    assert by_id["nominal-7"]["verdict"]["receipt"]["passed"] is True
    # The viewer's own outputs never count as tampering when the page is rebuilt in place.
    # 原地重建页面时，浏览器自己的输出不能被当作篡改。
    assert viewer.main([str(tmp_path), "--root", str(ROOT), "--cases", "nominal-7"]) == 0
    assert page_data(tmp_path / "viewer.html")["cases"][0]["integrity"]["receipt_digests"]["extra"] == []


def test_embedded_data_cannot_terminate_the_script_element(tmp_path):
    case = make_case(tmp_path, note="</script><script>alert(1)</script>")
    page = viewer.build_page([case], root=ROOT, output=tmp_path / "page.html")
    html = page.read_text(encoding="utf-8")
    assert "</script><script>alert(1)" not in html
    assert page_data(page)["cases"][0]["identity"]["randomization"]["note"] == "</script><script>alert(1)</script>"


def test_page_is_self_contained():
    template = viewer.TEMPLATE.read_text(encoding="utf-8")
    assert "http://" not in template and "https://" not in template
    assert "__DATA__" in template and "__VIEWER_VERSION__" in template
