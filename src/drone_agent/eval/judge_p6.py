"""Independent judge for one P6 case (D078): every P4 check plus the recapture invariants and target-region quality.

The judge never reuses the business engine, the workflow engine, the quality layer or the service. From the exported
tables, the transcript, the injections, the appearance history (with each capture's media digest) and the stored
media it re-derives that: a recapture mission exists only for a recapture node whose analysis failed for a reason
the pinned `recapture-v1` allows, at most one per node, for the same robot, volume and asset as the original
inspection, with its request naming the refusal; no finding, review or order follows a selection that found no
usable capture; each completed selection took the first completed analysis; and every job under a `quality-v2`
profile was refused, or passed, as the judge's own recomputation of the target geometry and pixels says (values
within a small band of a limit are inconclusive, never counted). The P4, P2 and P1 checks run on the same case.
Metrics (recaptures, completions after a recapture, false suspicions per template) are reported, not judged.

单个 P6 用例的独立裁判（D078）：P4 的全部检查，加上补拍不变量与目标区域质量。

裁判从不复用业务引擎、工作流引擎、质量层或服务。它从导出表、API 记录、注入、外观历史（含每次采集的媒体摘要）与存储的
媒体重新推导：补拍任务只属于其分析以固定的 `recapture-v1` 允许的原因失败的补拍节点，每个节点至多一个，机器人、体积与
资产与原巡检相同，请求写明拒判；没有可用采集的选择节点之后不产生发现、复核或工单；每个完成的选择节点取的是首个完成的
分析；每个 `quality-v2` 画像下的作业，其拒判或通过都与裁判自行复算的目标几何与像素一致（离限值很近的值记为无结论，
从不计数）。P4、P2、P1 的检查在同一用例上运行。指标（补拍数、补拍后完成数、各模板的误报）只报告，不判定。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

import numpy as np
import yaml

from drone_agent.eval.judge_p4 import COUNTS as P4_COUNTS
from drone_agent.eval.judge_p4 import Case as P4Case
from drone_agent.eval.judge_p4 import S1Case as P4S1Case
from drone_agent.eval.judge_p4 import _at, _json, business_counts, check_world, verdict
from drone_agent.fleet.business_models import load_quality

WORKFLOWS = "configs/workflows/p6_campus_v1.yaml"
MEMBERS = "configs/sites/p4_members_s0.yaml"
BUSINESS = "configs/analysis/p6_campus_v1.yaml"
S1_WORKFLOWS = "configs/workflows/p6_s1_v1.yaml"
S1_MEMBERS = "configs/sites/p4_members_s1.yaml"
S1_BUSINESS = "configs/analysis/p6_s1_v1.yaml"
COUNTS = (*P4_COUNTS, "unallowed_recapture", "excess_recapture", "recapture_mismatch", "refused_capture_verdict",
          "selection_mismatch", "quality_mismatch")
METRICS = ("recaptures", "recaptured_completions", "failed_after_recapture", "false_suspected_control",
           "false_suspected_recapture")
TARGET_REASONS = ("quality.target_unknown", "quality.target_out_of_frame", "quality.target_too_small",
                  "quality.target_off_center", "quality.target_exposure", "quality.target_blurry")
FRAME_REASONS = ("quality.resolution", "quality.exposure", "quality.blurry")
# Relative band around a limit inside which a recomputed value is inconclusive. / 复算值离限值的相对带宽，带内无结论。
BAND = 0.03


class Case(P4Case):
    """An S0 case with the P6 catalogs. / 使用 P6 目录的 S0 用例。"""

    workflows_path, members_path, business_path = WORKFLOWS, MEMBERS, BUSINESS


class S1Case(P4S1Case):
    """A PX4 SITL case with the P6 S1 catalogs. / 使用 P6 S1 目录的 PX4 SITL 用例。"""

    workflows_path, members_path, business_path = S1_WORKFLOWS, S1_MEMBERS, S1_BUSINESS


def _key(run_id: str, node_id: str) -> str:
    return f"wf:{run_id}:{node_id}:1"


def _request(c, run_id: str, node_id: str) -> list[dict]:
    return [r for r in c.requests if r["idempotency_key"] == _key(run_id, node_id)]


def _spec(c, run: dict):
    return c.templates.spec(run["project_id"], run["workflow_id"], run["version"])


def check_recaptures(c, problems: list[str]) -> tuple[int, int, int]:
    """(unallowed, excess, mismatched) recapture missions. / （不允许、超额、不一致）的补拍任务。"""
    unallowed = excess = mismatched = 0
    policy = c.business_catalog.recapture
    keyed = set()
    for run_id, run in c.runs.items():
        spec = _spec(c, run)
        nodes = c.nodes.get(run_id, {})
        for node in spec.nodes if spec is not None else ():
            target = getattr(node.params, "recapture_of", None)
            if target is None:
                continue
            requests = _request(c, run_id, node.node_id)
            keyed.update(r["request_id"] for r in requests)
            if len(requests) > 1:
                excess += len(requests) - 1
                problems.append(f"recapture_requested_twice:{run_id}:{node.node_id}")
            if not requests:
                continue
            refused = nodes.get(target, {})
            detail = _json(refused.get("detail")) or {}
            if refused.get("state") != "failed" or policy is None or refused.get("reason") not in policy.reasons \
                    or detail.get("recapture") is not True:
                unallowed += 1
                problems.append(f"recapture_without_an_allowed_refusal:{run_id}:{refused.get('reason')}")
            original = spec.node(spec.node(spec.node(target).params.inspection_from).params.mission_from)
            first = _request(c, run_id, original.node_id)
            body, again = _json(first[0]["body"]) if first else {}, _json(requests[0]["body"])
            same_robot = first and c.missions.get(first[0]["mission_id"], {}).get("robot_id") == \
                c.missions.get(requests[0]["mission_id"], {}).get("robot_id")
            if not first or not same_robot or body.get("asset_ids") != again.get("asset_ids") \
                    or body.get("approved_volume_id") != again.get("approved_volume_id"):
                mismatched += 1
                problems.append(f"recapture_differs_from_its_inspection:{run_id}")
            if f"after {refused.get('reason')}" not in again.get("text", ""):
                mismatched += 1
                problems.append(f"recapture_request_hides_its_reason:{run_id}")
    for request in c.requests:
        if ": recapture " in _json(request["body"]).get("text", "") and request["request_id"] not in keyed:
            unallowed += 1
            problems.append(f"recapture_outside_a_recapture_node:{request['request_id']}")
    return unallowed, excess, mismatched


def check_selections(c, problems: list[str]) -> tuple[int, int]:
    """(refused-capture verdicts, selection mismatches). / （拒判采集的结论，选择不一致）。"""
    verdicts = mismatches = 0
    for run_id, run in c.runs.items():
        spec = _spec(c, run)
        nodes = c.nodes.get(run_id, {})
        for node in spec.nodes if spec is not None else ():
            if node.activity != "select_analysis":
                continue
            row = nodes.get(node.node_id, {})
            done = [name for name in node.params.analyses if nodes.get(name, {}).get("state") == "completed"]
            if row.get("state") == "completed":
                result = _json(row.get("result")) or {}
                chosen = _json(nodes[done[0]]["result"]) if done else {}
                if not done or result.get("selected") != done[0] or \
                        result.get("analysis_id") != chosen.get("analysis_id"):
                    mismatches += 1
                    problems.append(f"selection_not_the_first_completed:{run_id}:{node.node_id}")
                continue
            if done:
                mismatches += 1
                problems.append(f"selection_ignored_a_completed_analysis:{run_id}:{node.node_id}")
            for other in spec.nodes:
                if getattr(other.params, "analysis_from", None) == node.node_id \
                        and nodes.get(other.node_id, {}).get("state") == "completed":
                    verdicts += 1
                    problems.append(f"review_without_a_usable_capture:{run_id}:{other.node_id}")
    for finding in c.findings.values():
        for job in c.jobs.values():
            if job.get("finding_id") == finding["finding_id"] and job["state"] != "completed":
                verdicts += 1
                problems.append(f"finding_from_a_refused_capture:{job['job_id']}")
    return verdicts, mismatches


# ── independent quality-v2 recomputation / 独立复算 quality-v2 ──


def _scene(c, mission_id: str) -> dict:
    robot = c.missions.get(mission_id, {}).get("robot_id")
    site = c.catalog.robots[robot].site_id if robot in c.catalog.robots else None
    if site is None:
        return {}
    return yaml.safe_load((c.root / c.catalog.sites[site].scene).read_text(encoding="utf-8")) or {}


def _heading(scene: dict, asset: dict) -> float | None:
    """The judge's own reading of the approach direction: the last two points of the registered route.

    裁判自己读取的进近方向：登记航线的最后两点。
    """
    route = (scene.get("routes") or {}).get(asset.get("observation_route"))
    if not route or len(route) < 2:
        return None
    (ax, ay), (bx, by) = route[-2][:2], route[-1][:2]
    return None if (ax, ay) == (bx, by) else math.atan2(by - ay, bx - ax)


def _profile(c, job: dict):
    analyzer = c.templates.analyzers.get(job["inputs"]["analyzer"])
    relative = getattr(analyzer, "quality", None) or c.business_catalog.default_quality
    profile, sha = load_quality(c.root, relative)
    return profile if sha == job["inputs"]["quality_sha256"] else None


def _geometry(pose: dict | None, asset: dict, profile, width: int, height: int, heading: float | None) -> dict | None:
    """The judge's own target geometry under the profile's pose model: sampled visibility of points inside the
    widened square, and per-pixel tests for the core and the boundary ring at its own heading steps.

    裁判自己在画像位姿模型下的目标几何：外扩正方形内采样点的可见比例，以及按其自己的航向步长逐像素判定核心与边界环。
    """
    camera, model = profile.camera, profile.pose
    position = (pose or {}).get("position") or {}
    place, size = asset.get("position"), asset.get("size_m")
    try:
        px, py, pz = (float(position[k]) for k in "xyz")
        ax, ay, az = (float(v) for v in place)
        size = float(size)
    except (KeyError, TypeError, ValueError):
        return None
    if asset.get("camera_id") != camera.camera_id or (width, height) != (camera.width, camera.height) or size <= 0:
        return None
    depth = pz - camera.mount_below_m - az
    if depth < 0.3:
        return None
    if model.heading == "approach_route":
        if heading is None:
            return None
        count = max(2, int(model.heading_tolerance_deg * 2)) + 1
        angles = np.linspace(heading - math.radians(model.heading_tolerance_deg),
                             heading + math.radians(model.heading_tolerance_deg), count)
    else:
        angles = np.radians(np.arange(0, 360, 2.0))
    scale = (camera.width / 2) / math.tan(camera.hfov_rad / 2) / depth  # pixels per ground metre / 每地面米的像素数
    dx, dy, half, slack = ax - px, ay - py, size / 2, model.position_tolerance_m
    grid = (np.arange(40) + 0.5) / 40 * 2 - 1
    sx, sy = np.meshgrid(grid * (half + slack) + dx, grid * (half + slack) + dy)
    rows, columns = np.indices((height, width))
    ahead = (height / 2 - rows - 0.5) / scale
    aside = (width / 2 - columns - 0.5) / scale
    worst, core, ring = 1.0, np.ones((height, width), bool), np.zeros((height, width), bool)
    for a in angles:
        cos, sin = math.cos(a), math.sin(a)
        u = width / 2 - scale * (-sx * sin + sy * cos)
        v = height / 2 - scale * (sx * cos + sy * sin)
        worst = min(worst, float(((u >= 0) & (u <= width) & (v >= 0) & (v <= height)).mean()))
        ex, ey = np.abs(ahead * cos - aside * sin - dx), np.abs(ahead * sin + aside * cos - dy)
        core &= (ex <= half - slack) & (ey <= half - slack)
        far = np.maximum(ex, ey)
        ring |= (far >= half - slack - 2 / scale) & (far <= half + slack + 2 / scale)
    return {"visible": worst, "side": scale * size, "core": core, "ring": ring[:-1, :-1]}


def _near(value: float, limit: float) -> bool:
    return abs(value - limit) <= BAND * max(abs(limit), 1.0)


def judge_quality(image: np.ndarray | None, pose: dict | None, asset: dict, profile,
                  heading: float | None = None) -> tuple[str | None, bool]:
    """(the first failing check by the judge's recomputation, conclusive). / （裁判复算的首个不合格项，是否有结论）。"""
    height, width = (image.shape[:2] if image is not None else (profile.camera.height, profile.camera.width))
    geometry = _geometry(pose, asset, profile, width, height, heading)
    if geometry is None:
        return "quality.target_unknown", True
    if image is None:
        return None, False
    rgb = image.astype(np.float64)
    luminance = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    gradient = np.hypot(np.abs(np.diff(luminance, axis=1))[:-1, :], np.abs(np.diff(luminance, axis=0))[:, :-1])
    sharp, mean = float(np.percentile(gradient, 99.5)), float(luminance.mean())
    clipped = float(((luminance <= 5) | (luminance >= 250)).mean())
    low, high = profile.luminance
    checks = [("quality.resolution", width < profile.min_width or height < profile.min_height, False),
              ("quality.exposure", not low <= mean <= high or clipped > profile.max_clipped_fraction,
               _near(mean, low) or _near(mean, high) or _near(clipped, profile.max_clipped_fraction)),
              ("quality.blurry", sharp < profile.min_sharpness, _near(sharp, profile.min_sharpness))]
    limits = profile.target
    checks.append(("quality.target_out_of_frame", geometry["visible"] < limits.min_visible_fraction,
                   _near(geometry["visible"], limits.min_visible_fraction)))
    checks.append(("quality.target_too_small", geometry["side"] < limits.min_side_px,
                   _near(geometry["side"], limits.min_side_px)))
    core = geometry["core"]
    checks.append(("quality.target_off_center", core.sum() < limits.min_core_px,
                   _near(float(core.sum()), float(limits.min_core_px))))
    if core.any():
        dark = float((image[core].max(axis=1) < limits.dark_level).mean())
        bright = float((image[core].min(axis=1) > limits.bright_level).mean())
        checks.append(("quality.target_exposure", dark > limits.max_dark_fraction or bright > limits.max_bright_fraction,
                       _near(dark, limits.max_dark_fraction) or _near(bright, limits.max_bright_fraction)))
        ring = geometry["ring"]
        edge = float(np.percentile(gradient[ring], 99.5)) if ring.any() else 0.0
        checks.append(("quality.target_blurry", edge < limits.min_sharpness, _near(edge, limits.min_sharpness)))
    for reason, failing, near in checks:
        if near:
            return reason if failing else None, False
        if failing:
            return reason, True
    return None, True


def check_pose_model(c, job: dict, profile, heading: float | None, problems: list[str]) -> int:
    """S1: the declared heading and position tolerances held at the capture, by the Gazebo truth nearest to it.

    S1：按离拍摄最近的 Gazebo 真值，声明的航向与位置容差在拍摄时成立。
    """
    truth = getattr(c, "truth", None)
    if not truth or profile.pose.heading != "approach_route" or heading is None:
        return 0
    evidence = job["inputs"]["evidence"]
    pose = ((c.evidence.get(evidence["evidence_id"], {}).get("body") or {}).get("captured_pose") or {}).get("position")
    captured = _at(evidence.get("captured_at"))
    rows = [r for rows in truth.values() for r in rows if r.get("timestamp") and r.get("orientation")]
    if not rows or captured is None or not pose:
        problems.append(f"pose_model_unverified:{job['job_id']}")
        return 0
    row = min(rows, key=lambda r: abs((_at(r["timestamp"]) - captured).total_seconds()))
    if abs((_at(row["timestamp"]) - captured).total_seconds()) > 0.5:
        problems.append(f"pose_model_unverified:{job['job_id']}")
        return 0
    x, y, z, w = row["orientation"]
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    off = math.degrees(math.atan2(math.sin(yaw - heading), math.cos(yaw - heading)))
    gap = math.hypot(row["position"][0] - pose["x"], row["position"][1] - pose["y"])
    if abs(off) > profile.pose.heading_tolerance_deg or gap > profile.pose.position_tolerance_m:
        problems.append(f"pose_model_violated:{job['job_id']}:heading {off:.1f} deg, position {gap:.2f} m")
        return 1
    return 0


def check_quality(c, problems: list[str]) -> int:
    """Jobs under a `quality-v2` profile whose refusal or pass the judge's recomputation contradicts, or whose
    declared pose model the truth contradicts (S1).

    `quality-v2` 画像下、其拒判或通过与裁判复算相矛盾，或其声明的位姿模型与真值相矛盾（S1）的作业。
    """
    contradictions = 0
    for job in c.jobs.values():
        if job["state"] not in ("completed", "refused") or c.verdicts.get(job["inputs"]["evidence"]["evidence_id"]) \
                != "verified":
            continue
        profile = _profile(c, job)
        if profile is None or not hasattr(profile, "camera"):
            continue
        reason = ((job["result"] or {}).get("reasons") or [None])[0] if job["state"] == "refused" else None
        if reason is not None and reason not in (*TARGET_REASONS, *FRAME_REASONS):
            continue
        evidence = job["inputs"]["evidence"]
        row = c.evidence.get(evidence["evidence_id"], {})
        media = c.case / "service/media" / (row.get("media_path") or "")
        image = None
        if row.get("media_path") and media.is_file():
            raw = media.read_bytes()
            if hashlib.sha256(raw).hexdigest() == evidence["media_sha256"] and \
                    len(raw) == evidence["width"] * evidence["height"] * 3:
                image = np.frombuffer(raw, dtype=np.uint8).reshape(evidence["height"], evidence["width"], 3)
        scene = _scene(c, evidence["mission_id"])
        asset = (scene.get("assets") or {}).get(evidence["asset_id"], {})
        heading = _heading(scene, asset)
        expected, conclusive = judge_quality(image, (row.get("body") or {}).get("captured_pose"), asset, profile,
                                             heading)
        if conclusive and expected != reason:
            contradictions += 1
            problems.append(f"quality_contradicts_recomputation:{job['job_id']}:{reason}!={expected}")
        contradictions += check_pose_model(c, job, profile, heading, problems)
    return contradictions


# ── metrics and expectations / 指标与期望 ──


def capture_state(c, job: dict) -> dict | None:
    """What the world showed at a job's capture: by media digest in S0, by capture time in S1.

    作业采集时世界所呈现的内容：S0 按媒体摘要，S1 按采集时刻。
    """
    history = c.appearance.get("history", [])
    evidence = job["inputs"]["evidence"]
    by_digest = {h["sha256"]: h for h in history if h.get("sha256")}
    if by_digest:
        return by_digest.get(evidence["media_sha256"])
    captured = _at(evidence.get("captured_at"))
    state = None
    for entry in sorted((h for h in history if h.get("at")), key=lambda h: _at(h["at"])):
        if captured is not None and _at(entry["at"]) <= captured:
            state = entry
    return state


def metrics(c) -> dict:
    """Reported, never judged. / 只报告，从不判定。"""
    recaptures = completed = failed = 0
    for run_id, run in c.runs.items():
        spec = _spec(c, run)
        for node in spec.nodes if spec is not None else ():
            if getattr(node.params, "recapture_of", None) and _request(c, run_id, node.node_id):
                recaptures += 1
                completed += run["state"] == "completed"
                failed += run["state"] == "failed"
    false = Counter()
    for job in c.jobs.values():
        capture = capture_state(c, job)
        run = c.runs.get(job["inputs"].get("run_id") or "")
        if job["verdict"] == "suspected" and capture is not None and capture.get("state") == "normal" and run:
            false[run["workflow_id"]] += 1
    control = sum(n for workflow, n in false.items() if not workflow.startswith("recapture"))
    return {"recaptures": recaptures, "recaptured_completions": completed, "failed_after_recapture": failed,
            "false_suspected_control": control, "false_suspected_recapture": sum(false.values()) - control,
            "false_suspected": dict(sorted(false.items()))}


def check_expected_p6(c, problems: list[str], found: dict) -> None:
    expected = c.scenario.get("expected", {})
    if "recaptures" in expected and found["recaptures"] != expected["recaptures"]:
        problems.append(f"recaptures {found['recaptures']} != {expected['recaptures']}")
    if "selected" in expected:
        chosen = sorted((_json(row.get("result")) or {}).get("selected") for nodes in c.nodes.values()
                        for row in nodes.values() if row["activity"] == "select_analysis"
                        and row["state"] == "completed")
        if chosen != sorted(expected["selected"]):
            problems.append(f"selected {chosen} != {sorted(expected['selected'])}")
    if "false_suspected" in expected:
        got = {k: found["false_suspected"].get(k, 0) for k in expected["false_suspected"]}
        if got != expected["false_suspected"]:
            problems.append(f"false_suspected {got} != {expected['false_suspected']}")


def p6_counts(c, problems: list[str], counts: dict) -> dict:
    unallowed, excess, mismatched = check_recaptures(c, problems)
    verdicts, selections = check_selections(c, problems)
    return {**counts, "unallowed_recapture": unallowed, "excess_recapture": excess,
            "recapture_mismatch": mismatched, "refused_capture_verdict": verdicts, "selection_mismatch": selections,
            "quality_mismatch": check_quality(c, problems)}


def judge_case(case: Path, root: Path, *, use_replay: bool = False) -> dict:
    from drone_agent.eval.judge_p1 import check_flights

    c = Case(case, root)
    problems: list[str] = []
    flight_false = check_flights(c, problems, use_replay=use_replay)
    counts = p6_counts(c, problems, business_counts(c, problems, use_replay=use_replay, flight_false=flight_false))
    found = metrics(c)
    check_expected_p6(c, problems, found)
    return verdict(c, problems, counts, case, layer="S0", use_replay=use_replay, metrics=found)


def judge_s1_case(case: Path, root: Path, *, use_replay: bool = False) -> dict:
    """The M2 flight judge per mission against Gazebo truth, then the P1, P2, P4 and P6 checks.

    每个任务用 M2 飞行裁判对 Gazebo 真值核对，再做 P1、P2、P4 与 P6 检查。
    """
    import tempfile

    from drone_agent.eval.judge_m2 import judge_case as judge_flight
    from drone_agent.eval.judge_p2 import mission_cases

    c = S1Case(case, root)
    problems: list[str] = []
    flights, flight_false = {}, 0
    with tempfile.TemporaryDirectory() as work:
        for mission_id, sub in mission_cases(c, Path(work)).items():
            result = judge_flight(sub, root, use_replay=use_replay)
            flights[mission_id] = {k: result.get(k) for k in ("classification", "passed", "false_success_reports",
                                                               "problems", "flown_versions", "mission_status",
                                                               "truly_inspected")}
            flight_false += result["false_success_reports"]
            problems += [f"{mission_id}:{p}" for p in result["problems"]]
    counts = p6_counts(c, problems, business_counts(c, problems, use_replay=use_replay, flight_false=flight_false))
    counts["false_closure"] += check_world(c, problems)
    found = metrics(c)
    check_expected_p6(c, problems, found)
    return verdict(c, problems, counts, case, layer="S1", use_replay=use_replay, metrics=found,
                   flight_judges=flights, missions=len(c.requests))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--layer", choices=["s0", "s1"], default="s0")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    judge = judge_s1_case if args.layer == "s1" else judge_case
    try:
        result = judge(args.case, args.root, use_replay=args.replay)
    except Exception as error:  # a judge failure is a failed case, never a pass / 裁判失败即用例失败
        result = {"passed": False, "classification": "unsafe_or_incorrect", "error": f"{type(error).__name__}:{error}"}
    if args.output is not None:
        from drone_agent.runtime.ledger import canonical

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical(result))
    print(json.dumps({k: v for k, v in result.items() if k != "artifacts"}, indent=2, default=str))
    raise SystemExit(0 if result.get("passed") else 1)


if __name__ == "__main__":
    main()
