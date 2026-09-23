"""Offline evidence viewer: one read-only page per recorded run, built from the artifacts the judge reads.

The viewer never produces a verdict. It renders what the executive, guardian, truth collector and
judge recorded, recomputes file digests so a human can see whether the evidence is intact, and
marks anything a run did not record as absent instead of guessing. Extractors write into four
generic primitives (series, lanes, tables, gallery), so later milestones add sections by adding
extractors, without rewriting the page.

离线证据浏览器：对每次记录的运行生成一页只读页面，读取的正是裁判读取的那些产物。

浏览器不产生任何判定。它呈现 executive、guardian、真值采集器与裁判记录下来的内容，重新计算
文件摘要让人能看到证据是否完整，缺失的产物一律标注为缺席而不是猜测。提取器把内容写入四种
通用原语（序列、事件带、表格、图集），后续里程碑只需增加提取器，不必重写页面。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path

from drone_agent.mission.registry import Registry, distance
from drone_agent.runtime.ledger import read_log
from drone_agent.runtime.recording import replay

VIEWER_VERSION = "0.1.0"
TEMPLATE = Path(__file__).with_name("viewer_template.html")

# The judge leaves these out of its digest map; the viewer follows the same rule and also ignores its
# own outputs, so a page regenerated in place never counts as tampered evidence.
# 裁判的摘要图排除这些文件；浏览器沿用同一规则并忽略自己的输出，原地重生成页面不会被当作篡改。
UNDIGESTED_NAMES = frozenset({"compose.log", "viewer.html", "fetch.json", "receipt.json"})

# PX4 vehicle_status.nav_state and arming_state, as read from ULog by sim/read_ulog.py.
# PX4 vehicle_status 的 nav_state 与 arming_state，来源是 sim/read_ulog.py 从 ULog 提取的记录。
PX4_NAV_STATES = {
    0: "MANUAL", 1: "ALTCTL", 2: "POSCTL", 3: "AUTO_MISSION", 4: "AUTO_LOITER", 5: "AUTO_RTL", 6: "POSITION_SLOW",
    10: "ACRO", 12: "DESCEND", 13: "TERMINATION", 14: "OFFBOARD", 15: "STAB", 17: "AUTO_TAKEOFF", 18: "AUTO_LAND",
    19: "AUTO_FOLLOW_TARGET", 20: "AUTO_PRECLAND", 21: "ORBIT", 22: "AUTO_VTOL_TAKEOFF",
}
PX4_ARMING_STATES = {1: "DISARMED", 2: "ARMED"}

FILE_HINTS = (
    (".ulg", "PX4 原始飞行日志：用 Flight Review 或 PlotJuggler 打开 / PX4 flight log: open with Flight Review or PlotJuggler"),
    (".mcap", "MCAP 记录：本页读取 flight/observation 与 mission/events / MCAP recording read by this page"),
    (".jsonl", "哈希链账本：本页已校验并展开 / hash-chained journal, verified and expanded here"),
    (".rgb", "原始 RGB 帧：本页无损转为 PNG 展示 / raw RGB frame rendered losslessly as PNG here"),
)

# Events the guardian journal records for authority, safety and injection; everything else is generic.
# guardian 账本中属于控制权、安全与注入的事件种类；其余按通用方式展示。
AUTHORITY_KINDS = frozenset({"lease", "intent", "receipt", "command_rejected"})
SAFETY_KINDS = frozenset(
    {"safety_intervention", "recovery_intent", "recovery_receipt", "recovered_to", "cancel_requested",
     "resume_authorized", "pause_requested", "takeover"}
)
ALERT_KINDS = frozenset(
    {"safety_intervention", "command_rejected", "executive_error", "fault_injected", "cancel_requested", "takeover"}
)


def sha256_file(path: Path) -> str:
    """Streamed SHA-256 of one file. / 逐块计算单个文件的 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path):
    """JSON content or None when the artifact is absent. / 文件存在时返回 JSON 内容，缺席返回 None。"""
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def stamp(value: str) -> float:
    """ISO-8601 (with Z or offset) to UTC epoch seconds. / ISO-8601（Z 或偏移）转 UTC 秒。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="milliseconds")


def png_data_uri(raw: bytes, width: int, height: int) -> str:
    """Encode exact RGB bytes as a lossless PNG data URI. / 将原始 RGB 字节无损编码为 PNG data URI。"""
    if len(raw) != width * height * 3:
        raise ValueError("raw RGB size does not match declared dimensions")

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    scanlines = b"".join(b"\0" + raw[y * width * 3 : (y + 1) * width * 3] for y in range(height))
    body = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0))
    body += chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(body).decode()


def _journal(path: Path) -> tuple[list[dict], str | None]:
    """Read a hash-chained journal; on integrity failure keep raw rows but report the error.

    读取哈希链账本；完整性失败时保留原始行，但报告错误。
    """
    if not path.is_file():
        return [], None
    try:
        return read_log(path), None
    except ValueError as error:
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                break
        return rows, str(error)


def _label(kind: str, data: dict) -> str:
    """Human label for a journal row; unknown kinds fall back to their name. / 账本行的可读标签；未知种类退回原名。"""
    key = (data.get("envelope") or {}).get("key") or {}
    outcome = data.get("outcome") or {}
    labels = {
        "mission_accepted": "任务包已接受",
        "command_submitted": f"提交 {key.get('step_id', '')}",
        "step_outcome": (
            f"{outcome.get('step_id', '')}: {outcome.get('execution_status', '?')} / "
            f"{outcome.get('effect_verdict', '?')} / {outcome.get('safety_verdict', '?')}"
        ),
        "skill_state": f"{data.get('step_id', '')} {data.get('previous', '?')} → {data.get('state', '?')}",
        "mission_result": "任务完成" if data.get("completed") else "任务未完成，进入安全收尾",
        "operator_request": f"操作者请求 {data.get('action', '')}",
        "operator_rejected": f"操作者请求被拒 {data.get('action', '')}: {data.get('reason', '')}",
        "command_rejected": f"命令被拒 {data.get('reason', '')}",
        "command_reconciled": f"对账 {data.get('step_id', data.get('key', ''))} {data.get('receipt', data.get('status', ''))}",
        "executive_error": f"executive 错误 {data.get('type', '')}",
        "lease": f"租约 epoch {data.get('lease_epoch', '?')}",
        "intent": f"意图 {key.get('step_id', '')} seq {(data.get('envelope') or {}).get('command_seq', '?')}",
        "receipt": f"回执 {data.get('receipt', '')}",
        "safety_intervention": f"安全干预 {data.get('reason', '')} → {data.get('behavior', '')}",
        "recovery_intent": f"恢复意图 {data.get('behavior', '')}",
        "recovery_receipt": f"恢复回执 {data.get('behavior', '')} {data.get('status', '')}",
        "recovered_to": f"已恢复到 {data.get('state', '')}",
        "cancel_requested": "取消请求已登记",
        "resume_authorized": "显式恢复已授权",
        "fault_injected": f"注入 {(data.get('injection') or {}).get('kind', '')}",
    }
    return labels.get(kind, kind)


def _level(kind: str, data: dict) -> str:
    if kind in ALERT_KINDS:
        return "alert"
    if kind == "step_outcome" and (data.get("outcome") or {}).get("effect_verdict") != "verified":
        return "warn"
    if kind == "mission_result" and not data.get("completed"):
        return "warn"
    if kind in {"skill_state", "receipt", "command_reconciled"}:
        return "minor"
    return "info"


def _lane(source: str, kind: str) -> str:
    if kind == "fault_injected":
        return "injection"
    if source == "executive":
        return "operator" if kind == "operator_request" else "mission"
    if kind in AUTHORITY_KINDS:
        return "authority"
    if kind in SAFETY_KINDS:
        return "safety"
    return "guardian"


def _observations(path: Path) -> list[dict]:
    """Deduplicated guardian observations from MCAP; unreadable files yield nothing.

    从 MCAP 读取去重后的 guardian 观测；文件不可读时返回空。
    """
    if not path.is_file():
        return []
    rows, seen = [], set()
    try:
        for topic, value in replay(path):
            if topic != "flight/observation" or value.get("sample_id") in seen or not value.get("pose"):
                continue
            seen.add(value.get("sample_id"))
            rows.append(value)
    except Exception:  # noqa: BLE001 - a corrupt recording is reported, never fatal / 损坏的记录只报告，不中断
        return []
    return rows


def _digest_map(run: Path) -> dict[str, str]:
    """Same coverage rule as the judge: every file except judge outputs and the viewer's own files.

    与裁判相同的覆盖规则：除裁判输出与浏览器自身文件外的全部文件。
    """
    return {
        path.relative_to(run).as_posix(): sha256_file(path)
        for path in sorted(run.rglob("*"))
        if path.is_file() and "judge" not in path.parts and path.name not in UNDIGESTED_NAMES
        and not path.name.startswith("viewer")
    }


def _compare(actual: dict[str, str], expected: dict[str, str] | None) -> dict:
    if expected is None:
        return {"present": False, "checked": 0, "matched": 0, "mismatched": [], "missing": [], "extra": []}
    mismatched = sorted(name for name in expected if name in actual and actual[name] != expected[name])
    missing = sorted(name for name in expected if name not in actual)
    extra = sorted(name for name in actual if name not in expected)
    return {
        "present": True,
        "checked": len(expected),
        "matched": len(expected) - len(mismatched) - len(missing),
        "mismatched": mismatched,
        "missing": missing,
        "extra": extra,
    }


def load_run(run: Path, root: Path, *, expected: dict | None = None, scene: Path | None = None,
             flight: Path | None = None, package_path: Path | None = None, service_view: dict | None = None,
             label: str | None = None) -> dict:
    """Assemble one case record from a run directory; every section reports presence explicitly.

    M2 cases pass one mission version's flight directory, its inbox package and the exported service view;
    M1 runs keep the single `aircraft/` directory and `input/package.json`.

    从运行目录组装一条记录；每个部分都显式报告是否存在。M2 用例传入某个任务版本的飞行目录、inbox 任务包与导出的
    服务视图；M1 运行仍使用单一 `aircraft/` 目录与 `input/package.json`。
    """
    run = run.resolve()
    aircraft = (flight or run / "aircraft").resolve()
    absent: list[str] = []
    notes: list[str] = []
    scenario = read_json(run / "input/scenario.json") or {}
    package = read_json(package_path or run / "input/package.json") or {}
    described = scenario.get("scenario") if isinstance(scenario.get("scenario"), dict) else {
        "id": scenario.get("scenario"), "expected": (scenario.get("expected") or {}).get("classification")}
    identity = {
        "case": label or run.name,
        "scenario": described.get("id"),
        "expected": described.get("expected"),
        "seed": scenario.get("seed"),
        "source_sha": scenario.get("source_sha"),
        "registry_hash": scenario.get("registry_hash"),
        "requested_speed_factor": scenario.get("requested_speed_factor"),
        "randomization": {k: v for k, v in scenario.items() if k not in {"scenario", "seed", "source_sha",
                                                                         "registry_hash", "requested_speed_factor"}},
        "mission_id": package.get("mission_id"),
        "mission_version": package.get("mission_version"),
        "recovery_policy_ref": package.get("recovery_policy_ref"),
        "robots": sorted({node.get("robot_id") for node in package.get("nodes", []) if node.get("robot_id")}),
    }
    capabilities = read_json(aircraft / "capabilities.json")
    if capabilities:
        identity["platform"] = {k: capabilities.get(k) for k in ("robot_id", "embodiment", "vendor", "model")}
        identity["control_modes"] = sorted(capabilities.get("control_modes", []))
    else:
        absent.append("aircraft/capabilities.json")
    for role in ("executive", "guardian"):
        record = read_json(aircraft / f"{role}-identity.json")
        if record:
            identity[f"{role}_pid"] = record.get("pid")
            identity.setdefault("recorded_sha", record.get("source_sha"))

    # World geometry comes from the registry the runtime used; a hash mismatch is shown, not hidden.
    # 世界几何来自运行时使用的登记表；哈希不一致时显示出来而不是隐藏。
    world: dict = {"present": False}
    try:
        registry = Registry(root, scene)
        data = registry.data
        world = {
            "present": True,
            "registry_id": data.get("registry_id"),
            "frame": data.get("frame"),
            "approved_volume_id": data.get("approved_volume_id"),
            "bounds": data.get("bounds"),
            "routes": data.get("routes", {}),
            "home": data.get("home"),
            "landing_sites": data.get("landing_sites", {}),
            "assets": data.get("assets", {}),
            "registry_hash": registry.sha256,
            "matches_run": (registry.sha256 == identity["registry_hash"]) if identity["registry_hash"] else None,
            "supervision": data.get("supervision", {}),
        }
        if world["matches_run"] is False:
            notes.append("登记表哈希与运行记录不一致：世界几何只作示意 / registry hash differs from the run; geometry is indicative only")
    except Exception as error:  # noqa: BLE001 - the page must still build without a registry / 没有登记表页面也要能生成
        notes.append(f"无法加载登记表 / registry unavailable: {type(error).__name__}")

    truth_rows = []
    truth_path = run / "truth/truth.jsonl"
    if truth_path.is_file():
        truth_rows = [json.loads(line) for line in truth_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        absent.append("truth/truth.jsonl")
    observations = _observations(aircraft / "guardian.mcap") or _observations(aircraft / "executive.mcap")
    if not observations:
        absent.append("aircraft/guardian.mcap flight/observation")
    executive_rows, executive_error = _journal(aircraft / "executive.jsonl")
    guardian_rows, guardian_error = _journal(aircraft / "guardian.jsonl")
    if not executive_rows:
        absent.append("aircraft/executive.jsonl")
    if not guardian_rows:
        absent.append("aircraft/guardian.jsonl")

    starts = [stamp(row["timestamp"]) for row in truth_rows[:1] + executive_rows[:1] + guardian_rows[:1]]
    starts += [stamp(value["timestamp"]) for value in observations[:1]]
    request = (service_view or {}).get("request") or {}
    if isinstance(request.get("received_at"), str):
        starts.append(stamp(request["received_at"]))
    t0 = min(starts) if starts else 0.0

    series = []
    if truth_rows:
        series.append({
            "id": "truth", "name": "Gazebo 真值", "kind": "truth",
            "columns": ["t", "x", "y", "z", "sim_time"],
            "points": [[round(stamp(r["timestamp"]) - t0, 3), *r["position"][:3], r.get("sim_time")] for r in truth_rows],
        })
    if observations:
        points = []
        for value in observations:
            position = value["pose"]["position"]
            covariance = position.get("covariance") or []
            sigma = round(max(covariance[0], covariance[4]) ** 0.5, 3) if len(covariance) == 9 else None
            points.append([
                round(stamp(value["timestamp"]) - t0, 3), position["x"], position["y"], position["z"], sigma,
                value.get("in_air"), value.get("armed"), value.get("flight_mode"), value.get("battery_fraction"),
                value.get("localization_healthy"),
            ])
        series.append({
            "id": "estimate", "name": "机载估计（guardian 观测）", "kind": "estimate",
            "columns": ["t", "x", "y", "z", "sigma_xy_m", "in_air", "armed", "flight_mode", "battery", "localization_ok"],
            "points": points,
        })

    events = []
    for source, rows in (("executive", executive_rows), ("guardian", guardian_rows)):
        for row in rows:
            data = row.get("data") or {}
            kind = row.get("kind", "?")
            events.append({
                "t": round(stamp(row["timestamp"]) - t0, 3), "seq": row.get("seq"), "source": source, "kind": kind,
                "lane": _lane(source, kind), "label": _label(kind, data), "level": _level(kind, data), "data": data,
            })
    harness = []
    for name, path in (("injection.json", run / "injection.json"), ("input/fault.json", run / "input/fault.json"),
                       ("aircraft/operator.json", aircraft / "operator.json"),
                       ("manual-cleanup.json", run / "manual-cleanup.json"), ("aircraft/probe.json", aircraft / "probe.json"),
                       ("mailbox/operator.json", run / "mailbox/operator.json"),
                       ("manual-cleanup.json", aircraft / "manual-cleanup.json")):
        value = read_json(path) if path.is_file() else None
        if value is not None:
            harness.append({"file": name, "data": value})
            if isinstance(value, dict) and isinstance(value.get("timestamp"), str):
                events.append({
                    "t": round(stamp(value["timestamp"]) - t0, 3), "seq": None, "source": "harness", "kind": name,
                    "lane": "injection", "label": f"{name}: {value.get('kind', value.get('action', value.get('reason', '')))}",
                    "level": "alert", "data": value,
                })
    for when, text, level in _planning_events(service_view):
        events.append({"t": round(stamp(when) - t0, 3), "seq": None, "source": "service", "kind": "planning",
                       "lane": "planning", "label": text, "level": level, "data": {}})
    events.sort(key=lambda e: (e["t"], e["seq"] if e["seq"] is not None else -1))

    steps = []
    for node in package.get("nodes", []):
        step_id = node.get("task_id")
        started = next((e["t"] for e in events if e["source"] == "executive" and e["kind"] == "command_submitted"
                        and ((e["data"].get("envelope") or {}).get("key") or {}).get("step_id") == step_id), None)
        outcome_event = next((e for e in events if e["source"] == "executive" and e["kind"] == "step_outcome"
                              and (e["data"].get("outcome") or {}).get("step_id") == step_id), None)
        steps.append({
            "id": step_id, "skill": node.get("skill_id"), "params": node.get("params", {}),
            "depends_on": node.get("depends_on", []), "timeout_s": node.get("timeout_s"),
            "resources": [claim.get("resource_id") for claim in node.get("resources", [])],
            "start": started, "end": outcome_event["t"] if outcome_event else None,
            "outcome": outcome_event["data"].get("outcome") if outcome_event else None,
            "states": [[e["t"], e["data"].get("previous"), e["data"].get("state")] for e in events
                       if e["kind"] == "skill_state" and e["data"].get("step_id") == step_id],
        })

    gallery = []
    for evidence_path in sorted(aircraft.glob("evidence-*.json")):
        evidence = read_json(evidence_path) or {}
        entry = {
            "step": evidence_path.stem.removeprefix("evidence-"), "asset_id": evidence.get("asset_id"),
            "t": round(stamp(evidence["capture_timestamp"]) - t0, 3) if evidence.get("capture_timestamp") else None,
            "timestamp": evidence.get("capture_timestamp"), "sim_time": evidence.get("sim_time"),
            "width": evidence.get("width"), "height": evidence.get("height"), "sha256": evidence.get("sha256"),
            "quality": (evidence.get("contract") or {}).get("quality"), "png": None, "sha_ok": None,
        }
        pose = ((evidence.get("contract") or {}).get("captured_pose") or {}).get("position") or {}
        if pose:
            entry["pose"] = [pose.get("x"), pose.get("y"), pose.get("z")]
            covariance = pose.get("covariance") or []
            entry["sigma_xy_m"] = round(max(covariance[0], covariance[4]) ** 0.5, 3) if len(covariance) == 9 else None
        media = aircraft / evidence.get("media_ref", "") if evidence.get("media_ref") else None
        if media and media.is_file():
            raw = media.read_bytes()
            entry["sha_ok"] = hashlib.sha256(raw).hexdigest() == evidence.get("sha256")
            try:
                entry["png"] = png_data_uri(raw, int(evidence["width"]), int(evidence["height"]))
            except (KeyError, ValueError, TypeError):
                entry["png"] = None
        # Truth near the capture in wall time first: a restarted simulator repeats sim_time values.
        # 先按墙钟取拍摄附近的真值：重启后的仿真器会重复 sim_time。
        nearby = [r for r in truth_rows if entry["timestamp"] and abs(stamp(r["timestamp"]) - stamp(entry["timestamp"])) < 2]
        if nearby and entry["sim_time"] is not None:
            nearest = min(nearby, key=lambda r: abs(r["sim_time"] - entry["sim_time"]))
            entry["truth_at_capture"] = nearest["position"][:3]
            entry["truth_gap_s"] = round(abs(nearest["sim_time"] - entry["sim_time"]), 3)
            asset = (world.get("assets") or {}).get(entry["asset_id"]) if world.get("present") else None
            if asset and asset.get("position"):
                entry["asset_position"] = asset["position"]
                entry["truth_distance_to_asset_m"] = round(distance(nearest["position"][:2], asset["position"][:2]), 3)
        outcome = next((s["outcome"] for s in steps if s["id"] == entry["step"] and s["outcome"]), None)
        entry["effect_verdict"] = outcome.get("effect_verdict") if outcome else None
        gallery.append(entry)

    judge = read_json(run / "judge/result.json")
    replayed = read_json(run / "judge/replay.json")
    verdict = {"judged": judge is not None}
    if judge:
        verdict.update({k: judge.get(k) for k in (
            "classification", "expected", "passed", "false_success_reports", "problems", "recovery_reasons",
            "validated_edge", "measured_sim_speed", "truth_samples", "sim_duration_s", "error")})
        keys = ("classification", "false_success_reports", "problems")
        verdict["replay"] = {
            "present": replayed is not None,
            "keys": {k: (judge.get(k) == replayed.get(k)) if replayed else None for k in keys},
            "agrees": all(judge.get(k) == replayed.get(k) for k in keys) if replayed else None,
        }
    else:
        absent.append("judge/result.json")
    if expected is not None:
        verdict["receipt"] = {k: expected.get(k) for k in ("passed", "classification", "replay_agrees", "judge_exit_code")}

    supervision = read_json(aircraft / "supervision.json") or {}
    supervision_view = {
        "present": bool(supervision), "samples": supervision.get("samples"), "p99_s": supervision.get("p99_s"),
        "max_s": supervision.get("max_s"), "budget_s": (world.get("supervision") or {}).get("period_s"),
    }
    if not supervision:
        absent.append("aircraft/supervision.json")
    status = read_json(aircraft / "status.json") or {}
    terminal = status.get("observation") or {}

    actual = _digest_map(run)
    mcap_matches = None
    mcap_path = aircraft / "executive.mcap"
    if mcap_path.is_file() and executive_rows and executive_error is None:
        try:
            replayed_events = [value for topic, value in replay(mcap_path) if topic == "mission/events"]
            mcap_matches = replayed_events == executive_rows
        except Exception:  # noqa: BLE001 - reported below as a failed check / 作为失败检查报告
            mcap_matches = False
    integrity = {
        "journals": {"executive": executive_error or ("ok" if executive_rows else "absent"),
                     "guardian": guardian_error or ("ok" if guardian_rows else "absent")},
        "mcap_replay_matches_journal": mcap_matches,
        "judge_digests": _compare(actual, judge.get("artifacts") if judge else None),
        "receipt_digests": _compare(actual, expected.get("artifacts") if expected else None),
        "files_hashed": len(actual),
    }

    tables = []
    fc_events = read_json(run / "judge/fc-events.json")
    if fc_events:
        rows, last = [], (None, None)
        for entry in fc_events.get("modes", []):
            current = (entry.get("nav_state"), entry.get("arming_state"))
            if current != last:
                rows.append([round(entry.get("timestamp", 0) / 1e6, 3), PX4_NAV_STATES.get(current[0], str(current[0])),
                             PX4_ARMING_STATES.get(current[1], str(current[1]))])
                last = current
        tables.append({
            "id": "fc_modes", "title": "飞控模式迁移（ULog，自启动秒数，不与 UTC 对齐）",
            "columns": ["t_boot_s", "nav_state", "arming"], "rows": rows,
        })
        acks = [[round(a.get("timestamp", 0) / 1e6, 3), a.get("command"), a.get("result")] for a in fc_events.get("acks", [])]
        tables.append({"id": "fc_acks", "title": "飞控命令确认（ULog）", "columns": ["t_boot_s", "command", "result"], "rows": acks})
    if harness:
        tables.append({
            "id": "harness", "title": "注入与操作者文件（仿真 harness 写入）", "columns": ["file", "content"],
            "rows": [[h["file"], json.dumps(h["data"], ensure_ascii=False)] for h in harness],
        })
    adapter_commands = read_json(aircraft / "adapter-commands.json")
    if isinstance(adapter_commands, list):
        tables.append({
            "id": "adapter", "title": "适配器控制写入（guardian 唯一出口）", "columns": ["t", "command"],
            "rows": [[round(stamp(c["timestamp"]) - t0, 3) if isinstance(c.get("timestamp"), str) else None,
                      json.dumps({k: v for k, v in c.items() if k != "timestamp"}, ensure_ascii=False)]
                     for c in adapter_commands],
        })

    tables += _service_tables(service_view)

    files = []
    for path in sorted(run.rglob("*")):
        if path.is_file():
            name = path.relative_to(run).as_posix()
            hint = next((text for suffix, text in FILE_HINTS if name.endswith(suffix)), "")
            files.append({"path": name, "size": path.stat().st_size, "hint": hint})

    duration = max([s["points"][-1][0] for s in series if s["points"]] + [e["t"] for e in events] + [0.0])
    return {
        "id": identity["case"], "path": str(run), "t0": t0, "t0_iso": iso(t0) if starts else None, "duration_s": round(duration, 3),
        "identity": identity, "world": world, "series": series, "events": events, "steps": steps, "gallery": gallery,
        "verdict": verdict, "supervision": supervision_view, "terminal": {
            "in_air": terminal.get("in_air"), "armed": terminal.get("armed"), "flight_mode": terminal.get("flight_mode"),
            "battery_fraction": terminal.get("battery_fraction"), "phase": status.get("phase"), "safety": status.get("safety"),
            "reason": status.get("reason"),
        },
        "integrity": integrity, "tables": tables, "files": files, "absent": absent, "notes": notes,
    }


FAILED_VERSION_STATES = frozenset({"rejected", "refused", "declined", "delivery_rejected", "planning_failed"})


def _planning_events(view: dict | None) -> list[tuple[str, str, str]]:
    """The planning band: request, each version's creation and approval, the report. / 规划事件带。"""
    if not view:
        return []
    request = view.get("request") or {}
    found = [(request.get("received_at"), f"请求 / request · {request.get('requested_by')}: {request.get('text', '')[:80]}",
              "info")]
    for version in view.get("versions", []):
        level = "alert" if version.get("status") in FAILED_VERSION_STATES else "info"
        found.append((version.get("created_at"), f"v{version['version']} {version.get('origin')} → {version.get('status')}",
                      level))
        approval = version.get("approval") or {}
        found.append((approval.get("approved_at"),
                      f"v{version['version']} 审批 / approved by {approval.get('approver')} · {approval.get('signer_key_id')}",
                      "info"))
    report = view.get("report") or {}
    found.append((report.get("generated_at"), f"报告 / report · {json.dumps(report.get('targets'), ensure_ascii=False)}",
                  "info"))
    return [item for item in found if isinstance(item[0], str)]


def _service_tables(view: dict | None) -> list[dict]:
    """Planning, approval, report, verification and issue tables from the service export. / 服务导出的各表。"""
    if not view:
        return []
    planning, approvals = [], []
    for v in view.get("versions", []):
        planner, admission = v.get("planner") or {}, v.get("admission") or {}
        planning.append([v["version"], v.get("origin"), v.get("status"),
                         f"{planner.get('provider_id')}/{planner.get('model_id')}" if planner else "—",
                         planner.get("prompt_version") or "—", (planner.get("input_hash") or "")[:16] or "—",
                         f"{planner.get('attempts')} · {','.join(planner.get('channels') or [])}" if planner else "—",
                         admission.get("accepted"), admission.get("energy_upper_fraction"),
                         ", ".join(admission.get("codes") or []) or "—"])
        approval = v.get("approval") or {}
        if approval:
            approvals.append([v["version"], approval.get("approver"), approval.get("approved_at"),
                              approval.get("expires_at"), approval.get("signer_key_id"), (v.get("package_hash") or "")[:16]])
    tables = [
        {"id": "planning", "title": "规划与准入（任务服务） / planning and admission",
         "columns": ["version", "origin", "status", "planner", "prompt", "input_hash", "attempts", "admitted",
                     "energy_upper", "codes"], "rows": planning},
        {"id": "approval", "title": "审批与签名 / approvals and signatures",
         "columns": ["version", "approver", "approved_at", "expires_at", "signer_key_id", "package_hash"],
         "rows": approvals},
    ]
    report = view.get("report")
    if report:
        tables.append({"id": "report", "title": "三列报告（服务生成，裁判另行核对） / three-column report",
                       "columns": ["version", "step", "column", "onboard", "service", "reason"],
                       "rows": [[r["mission_version"], r["step_id"], r["column"],
                                 f"{r.get('execution_status')} / {r.get('effect_verdict')}", r.get("service_verdict"),
                                 r.get("reason")] for r in report.get("rows", [])]})
    checked = [e for e in view.get("evidence", []) if e.get("verification")]
    if checked:
        tables.append({"id": "verification", "title": "证据复核（服务） / service evidence recheck",
                       "columns": ["version", "step", "evidence", "onboard", "service", "final", "agrees"],
                       "rows": [[e["version"], e["step_id"], e["evidence_id"][:22], e["verification"].get("onboard_verdict"),
                                 e["verification"].get("service_verdict"), e["verification"].get("final_verdict"),
                                 e["verification"].get("agrees")] for e in checked]})
    if view.get("issues"):
        tables.append({"id": "issues", "title": "问题码 / issues", "columns": ["code", "message"],
                       "rows": [[i.get("code"), i.get("message")] for i in view["issues"]]})
    return tables


def load_m2_case(case: Path, root: Path, *, expected: dict | None = None) -> list[dict]:
    """One record per flown mission version, or one record without a flight. / 每个已飞行的任务版本一条记录。"""
    from drone_agent.mission.registry import M2_SCENE

    view = read_json(case / "service-export/view.json")
    scene = root / M2_SCENE
    flights = []
    for mission in sorted((case / "aircraft").iterdir()) if (case / "aircraft").is_dir() else []:
        for version in sorted(mission.iterdir()) if mission.is_dir() else []:
            if version.is_dir() and version.name.startswith("v") and version.name[1:].isdigit():
                flights.append((mission.name, int(version.name[1:]), version))
    if not flights:
        return [load_run(case, root, expected=expected, scene=scene, flight=case / "aircraft" / "none",
                         package_path=case / "input" / "none.json", service_view=view, label=f"{case.name} · no flight")]
    return [load_run(case, root, expected=expected, scene=scene, flight=flight,
                     package_path=case / "inbox/history" / f"{mission}-v{version}.json", service_view=view,
                     label=f"{case.name} · v{version}") for mission, version, flight in sorted(flights, key=lambda f: f[1])]


def discover(path: Path) -> tuple[list[Path], dict | None]:
    """A single case directory, or a batch directory holding cases plus an optional receipt.

    单个用例目录，或包含多个用例与可选回执的批次目录。
    """
    path = path.resolve()
    if (path / "input/scenario.json").is_file():
        return [path], None
    cases = sorted(p for p in path.iterdir() if p.is_dir() and (p / "input/scenario.json").is_file())
    receipt = read_json(path / "receipt.json") or read_json(path / "progress.json")
    fetched = read_json(path / "fetch.json") or {}
    if receipt is not None and fetched.get("deployment_id") and not receipt.get("deployment_id"):
        # progress.json on the server carries no deployment id; the fetch record does.
        # 服务器上的 progress.json 不含部署 ID；拉取记录里有。
        receipt = {**receipt, "deployment_id": fetched["deployment_id"]}
    return cases, receipt


def receipt_results(receipt: dict | None) -> dict[str, dict]:
    """Index receipt rows by case directory name. / 按用例目录名索引回执行。"""
    if not receipt:
        return {}
    return {f"{row.get('scenario')}-{row.get('seed')}": row for row in receipt.get("results", [])}


def build_page(cases: list[Path], *, root: Path, output: Path, receipt: dict | None = None, scene: Path | None = None) -> Path:
    """Render the standalone HTML page for the given cases. / 为给定用例生成独立 HTML 页面。"""
    if not cases:
        raise ValueError("no case directories to render")
    expected = receipt_results(receipt)
    records = []
    for case in cases:
        if (case / "service-export").is_dir():
            records += load_m2_case(case, root, expected=expected.get(case.name))
        else:
            records.append(load_run(case, root, expected=expected.get(case.name), scene=scene))
    payload = {
        "viewer_version": VIEWER_VERSION,
        "generated_at": iso(datetime.now(tz=timezone.utc).timestamp()),
        "receipt": {k: receipt.get(k) for k in ("status", "source_sha", "deployment_id", "artifact_directory")} if receipt else None,
        "cases": records,
    }
    # `</` must not terminate the embedding script element. / `</` 不得提前结束承载数据的 script 元素。
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__VIEWER_VERSION__", VIEWER_VERSION).replace("__DATA__", data)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    return output


def default_root() -> Path:
    """Repository root when run from a checkout, else the current directory. / 检出仓库时为仓库根，否则为当前目录。"""
    for candidate in (Path.cwd(), Path(__file__).resolve().parents[3]):
        if (candidate / "configs/scenarios").is_dir():
            return candidate
    return Path.cwd()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="case directory or batch directory / 用例目录或批次目录")
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--receipt", type=Path, default=None, help="receipt JSON with per-case digests / 带各用例摘要的回执")
    parser.add_argument("--cases", default="all", help="comma-separated case names or all / 逗号分隔的用例名或 all")
    parser.add_argument("--scene", type=Path, default=None, help="registry file if not the default / 非默认登记表路径")
    args = parser.parse_args(argv)
    cases, receipt = discover(args.path)
    if args.receipt:
        receipt = read_json(args.receipt)
    if args.cases != "all":
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c.name in wanted]
    output = args.output or (args.path.resolve() / "viewer.html")
    result = build_page(cases, root=args.root, output=output, receipt=receipt, scene=args.scene)
    print(json.dumps({"viewer": str(result), "cases": [c.name for c in cases]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
