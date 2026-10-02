"""Independent judge for one P5 case (D071, D072): the road template and the vendor protocol contract.

A road case (S0) is judged exactly like a P4 business case, with the P5 catalogs: one open finding per cluster, one
order per finding, decisions and closures only by first-party reviewers, every closure passing `reinspection-v1`
recomputed by the judge, analyses only of verified evidence, nothing opened after a cancel, no project escape, and the
P1 dispatch, release and flight checks and the P2 workflow checks on the same records.

A vendor case (S3) never reuses the gateway. From the simulator's own truth log (every command it received, every
execution it actually started, every capture), the exported ledger tables and the hash-chained vendor journal folded
again here, the judge counts executions of one flight beyond the first, executions after a cancel intent, commands
outside the whitelist, missions reported completed without the vendor's terminal `ok`, the vendor's uploaded capture
and the service's own `verified` verdict on it, journal rows duplicated or out of chain, and reservations released
without the vendor's terminal and, after a flight, its landing in the dock. The `replay` mode rebuilds the journal from
the raw event bodies instead of the exported columns; both must agree.

单个 P5 用例的独立裁判（D071、D072）：道路模板与厂商协议合同。

道路用例（S0）与 P4 业务用例完全同样裁判，只是换成 P5 目录：每个聚合键一个未结发现、每个发现一张工单、决定与关单只依据第一方
reviewer、每次关单都通过裁判复算的 `reinspection-v1`、分析只针对已证实证据、取消后不开启任何东西、不越出项目，同一记录上另做
P1 派遣、释放与飞行检查和 P2 工作流检查。

厂商用例（S3）从不复用网关。裁判依据模拟器自己的真值日志（收到的每个命令、实际开始的每次执行、每次拍摄）、导出的账本表与在这里
重新折叠的哈希链厂商账本，计数：同一飞行第一次之后的执行、取消意图之后的执行、白名单之外的命令、在没有厂商终态 `ok`、厂商上传
的采集及服务自己对其 `verified` 判定时就报告完成的任务、重复或断链的账本行，以及在没有厂商终态、飞行后没有落回机场时就释放的
预约。`replay` 模式由原始事件体而不是导出列重建账本；两者必须一致。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from drone_agent.eval import judge_p4
from drone_agent.runtime.ledger import content_hash

CATALOG = "configs/sites/p5_campus_v1.yaml"
WORKFLOWS = "configs/workflows/p5_campus_v1.yaml"
MEMBERS = "configs/sites/p5_members_s0.yaml"
BUSINESS = "configs/analysis/p5_campus_v1.yaml"
S1_CATALOG = "configs/sites/p5_s1_v1.yaml"
S1_WORKFLOWS = "configs/workflows/p5_s1_v1.yaml"
S1_MEMBERS = "configs/sites/p5_members_s1.yaml"
S1_BUSINESS = "configs/analysis/p5_s1_v1.yaml"
WHITELIST = ("flighttask_prepare", "flighttask_execute", "flighttask_undo", "return_home")
VENDOR_COUNTS = ("duplicate_execution", "post_cancel_execution", "low_level_command", "false_success",
                 "unknown_as_success", "journal_defect", "wrong_release", "post_cancel_dispatch", "project_escape")
COUNTS = tuple(dict.fromkeys((*judge_p4.COUNTS, *VENDOR_COUNTS)))
TERMINAL_MISSION = ("completed", "incomplete", "declined", "rejected", "refused", "planning_failed",
                    "delivery_rejected", "cancelled", "dispatch_expired")


def _at(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


# ── road cases / 道路用例 ──


class RoadCase(judge_p4.Case):
    """An S0 road case: the P4 records with the P5 catalogs. / S0 道路用例：换成 P5 目录的 P4 记录。"""

    catalog_path, workflows_path, members_path, business_path = CATALOG, WORKFLOWS, MEMBERS, BUSINESS


def road_extra(c: RoadCase, problems: list[str]) -> None:
    """What the road template adds: one run per road report and the event refusals. / 道路模板的额外要求。"""
    expected = c.scenario.get("expected", {})
    if "events_sent" in expected.get("injections", []):
        sent = next((i for i in c.injections if i["kind"] == "events_sent"), None)
        triggered = [r for r in c.runs.values() if r["trigger_event"] == "report-dup"]
        if sent is None or len(triggered) != 1:
            problems.append(f"road report deduplication: {len(triggered)} runs")


def judge_road_case(case: Path, root: Path, *, use_replay: bool = False) -> dict:
    from drone_agent.eval.judge_p1 import check_flights

    c = RoadCase(case, root)
    problems: list[str] = []
    flight_false = check_flights(c, problems, use_replay=use_replay)
    counts = judge_p4.business_counts(c, problems, use_replay=use_replay, flight_false=flight_false)
    road_extra(c, problems)
    result = judge_p4.verdict(c, problems, counts, case, layer="S0", use_replay=use_replay)
    result["counts"] = {key: result["counts"].get(key, 0) for key in COUNTS}
    return result


# ── vendor cases / 厂商用例 ──


def chain_ok(rows: list[dict]) -> bool:
    previous = "0" * 64
    for index, row in enumerate(sorted(rows, key=lambda r: r["seq"])):
        body = {k: row[k] for k in ("seq", "previous", "timestamp", "monotonic_ns", "kind", "data")}
        if row["seq"] != index or row["previous"] != previous or content_hash(body) != row["sha256"]:
            return False
        previous = row["sha256"]
    return True


def journal_rows(table: list[dict], *, use_replay: bool) -> dict[tuple[str, int], list[dict]]:
    """The vendor journal per (mission, version), from the exported columns or rebuilt from the raw bodies.

    每个（任务，版本）的厂商账本：取自导出列，或由原始事件体重建。
    """
    grouped: dict[tuple[str, int], list[dict]] = {}
    for row in table:
        body = _json(row["body"])
        data = body["data"]
        if use_replay:
            rebuilt = {"seq": data["seq"], "previous": data["previous"], "timestamp": data["row_timestamp"],
                       "monotonic_ns": data["monotonic_ns"], "kind": data["kind"], "data": data["data"],
                       "sha256": data["sha256"]}
        else:
            rebuilt = {"seq": row["seq"], "previous": row["previous"], "timestamp": data["row_timestamp"],
                       "monotonic_ns": data["monotonic_ns"], "kind": row["kind"], "data": data["data"],
                       "sha256": row["sha256"]}
        grouped.setdefault((row["mission_id"], row["mission_version"]), []).append(rebuilt)
    return grouped


def fold(rows: list[dict]) -> dict:
    """The judge's own reading of one vendor journal. / 裁判自己对一份厂商账本的解读。"""
    found = {"terminal": None, "grounded_at": None, "commands": Counter(), "events": Counter(), "media": [],
             "flight_id": None, "cancel": None}
    for row in sorted(rows, key=lambda r: r["seq"]):
        kind, data = row["kind"], row["data"]
        if kind == "task":
            found["flight_id"] = data["flight_id"]
        elif kind == "command":
            found["commands"][data["method"]] += 1
        elif kind == "event":
            found["events"][(data["method"], data["seq"])] += 1
        elif kind == "terminal" and found["terminal"] is None:
            found["terminal"] = {"status": data["status"], "physical": data["physical"], "at": row["timestamp"]}
        elif kind == "grounded" and found["grounded_at"] is None:
            found["grounded_at"] = row["timestamp"]
        elif kind == "media":
            found["media"].append(data)
        elif kind == "cancel" and found["cancel"] is None:
            found["cancel"] = {"at": row["timestamp"], "via": data["via"]}
    return found


class VendorCase:
    """Everything recorded for one vendor case. / 一个厂商用例的全部记录。"""

    def __init__(self, case: Path, root: Path, *, use_replay: bool):
        self.case, self.root = case, root
        self.scenario = json.loads((case / "input/scenario.json").read_text(encoding="utf-8"))
        self.tables = json.loads((case / "service-export/business.json").read_text(encoding="utf-8"))
        self.truth = json.loads((case / "world/vendor.json").read_text(encoding="utf-8"))["truth"]
        self.injections = [json.loads(line) for line in (case / "world/injections.jsonl").read_text(
            encoding="utf-8").splitlines() if line.strip()]
        self.transcript = [json.loads(line) for line in (case / "world/api.jsonl").read_text(
            encoding="utf-8").splitlines() if line.strip()]
        self.bindings = {row["mission_id"]: row for row in self.tables["op_bindings"]}
        self.vendor = {m: b for m, b in self.bindings.items() if b["execution_backend"] == "vendor_protocol_sim"}
        self.missions = {row["mission_id"]: row for row in self.tables["missions"] if row["mission_id"] in self.vendor}
        self.journals = journal_rows(self.tables["vendor_journal"], use_replay=use_replay)
        self.cancels = {row["mission_id"]: row for row in self.tables["op_cancellations"]}
        self.runs = {row["run_id"]: row for row in self.tables["wf_runs"] if row["project_id"] == "vendor_ops"}


def check_vendor(c: VendorCase, problems: list[str]) -> dict:
    counts = dict.fromkeys(VENDOR_COUNTS, 0)
    executions = Counter(e["flight_id"] for e in c.truth["executions"])
    for flight, number in executions.items():
        if number > 1:
            counts["duplicate_execution"] += number - 1
            problems.append(f"flight {flight} executed {number} times")
    for command in c.truth["commands"]:
        if command["method"] not in WHITELIST:
            counts["low_level_command"] += 1
            problems.append(f"non-whitelisted command reached the vendor: {command['method']}")
    folded = {}
    for key, rows in c.journals.items():
        if not chain_ok(rows):
            counts["journal_defect"] += 1
            problems.append(f"vendor journal of {key[0]} v{key[1]} is not one chain")
        found = folded[key] = fold(rows)
        duplicated = [k for k, n in found["events"].items() if n > 1]
        if duplicated:
            counts["journal_defect"] += len(duplicated)
            problems.append(f"events journaled twice in {key[0]}: {duplicated[:3]}")
    flights_of = {found["flight_id"]: key for key, found in folded.items() if found["flight_id"]}
    for execution in c.truth["executions"]:
        key = flights_of.get(execution["flight_id"])
        if key is None:
            counts["false_success"] += 1
            problems.append(f"the vendor executed {execution['flight_id']} that no claimed task names")
            continue
        cancel = c.cancels.get(key[0])
        if cancel is not None and _at(execution["at"]) > _at(cancel["requested_at"]) + timedelta(seconds=0.5):
            counts["post_cancel_execution"] += 1
            problems.append(f"{key[0]} started executing after its cancel intent")
    verified = {row["evidence_id"] for row in c.tables["verifications"]
                if _json(row["body"]).get("final_verdict") == "verified"}
    evidence = {row["evidence_id"]: row for row in c.tables["evidence"]}
    uploaded = {cap["sha256"] for cap in c.truth["captures"] if cap["uploaded"]}
    terminals = {t["flight_id"]: t["status"] for t in c.truth["terminals"]}
    for mission_id, mission in c.missions.items():
        key = (mission_id, 1)
        found = folded.get(key)
        if mission["status"] == "completed":
            media = [m for m in (found or {}).get("media", []) if m.get("accepted")]
            ok = found is not None and found["terminal"] and found["terminal"]["status"] == "ok" and \
                terminals.get(found["flight_id"]) == "ok" and any(
                    m["evidence_id"] in verified and evidence.get(m["evidence_id"], {}).get("sha256") in uploaded
                    for m in media)
            if not ok:
                counts["false_success"] += 1
                problems.append(f"{mission_id} completed without the vendor's ok, its uploaded capture and a "
                                f"verified verdict")
            if found is None or found["terminal"] is None:
                counts["unknown_as_success"] += 1
        for claim in c.tables.get("op_claims", []):
            cancel = c.cancels.get(mission_id)
            if claim["mission_id"] == mission_id and claim["state"] == "claimed" and cancel is not None and \
                    _at(claim["decided_at"]) > _at(cancel["requested_at"]):
                counts["post_cancel_dispatch"] += 1
                problems.append(f"{mission_id} was claimed after its cancel intent")
    for reservation in c.tables["op_reservations"]:
        mission_id = reservation["mission_id"]
        if mission_id not in c.vendor or reservation["state"] != "released" or reservation["reason"] != "reconciled":
            continue
        found = folded.get((mission_id, reservation["mission_version"]))
        terminal = found["terminal"] if found else None
        delivered = next((d for d in c.tables["deliveries"] if d["mission_id"] == mission_id
                          and d["kind"] == "mission_package"), None)
        rejected = delivered is not None and delivered["acked_at"] and not delivered["ack_accepted"]
        if terminal is None and not rejected:
            counts["wrong_release"] += 1
            problems.append(f"{mission_id} released without a vendor terminal")
        elif terminal is not None and terminal["physical"] and found["grounded_at"] is None:
            counts["wrong_release"] += 1
            problems.append(f"{mission_id} released before the vendor reported it back in the dock")
    for row in c.transcript:
        if row["probe"] and (row["ok"] or row["result_sha256"] is not None):
            counts["project_escape"] += 1
            problems.append(f"probe_not_refused:{row['actor']}:{row['method']}")
    return counts


def check_vendor_expected(c: VendorCase, problems: list[str]) -> None:
    expected = c.scenario.get("expected", {})
    if c.scenario.get("harness_error"):
        problems.append(f"harness_error:{c.scenario['harness_error']}")
    if "executions" in expected and len(c.truth["executions"]) != expected["executions"]:
        problems.append(f"executions {len(c.truth['executions'])} != {expected['executions']}")
    if "missions" in expected:
        states = dict(Counter(m["status"] for m in c.missions.values()))
        if states != expected["missions"]:
            problems.append(f"missions {states} != {expected['missions']}")
    if "runs" in expected:
        states = dict(Counter(r["state"] for r in c.runs.values()))
        if states != expected["runs"]:
            problems.append(f"runs {states} != {expected['runs']}")
    if "released" in expected:
        states = [r["state"] for r in c.tables["op_reservations"] if r["mission_id"] in c.vendor]
        released = bool(states) and all(state == "released" for state in states)
        if released != expected["released"]:
            problems.append(f"vendor reservations {states}, expected released={expected['released']}")
    folded = {key: fold(rows) for key, rows in c.journals.items()}
    for method in expected.get("resent", []):
        if not any(found["commands"][method] >= 2 for found in folded.values()):
            problems.append(f"{method} was never resent")
    if "link_drops" in expected:
        drops = sum(1 for entry in c.truth["link"] if entry["state"] == "down")
        if drops != expected["link_drops"]:
            problems.append(f"link drops {drops} != {expected['link_drops']}")
    if "terminal" in expected:
        statuses = sorted({(f["terminal"] or {}).get("status") for f in folded.values()} - {None})
        if statuses != [expected["terminal"]]:
            problems.append(f"terminal {statuses} != {expected['terminal']}")
    if "issue" in expected:
        codes = {row["code"] for row in c.tables["issues"] if row["mission_id"] in c.vendor}
        if expected["issue"] not in codes:
            problems.append(f"issue {expected['issue']} missing (got {sorted(codes)})")
    if "refusals" in expected:
        refused = sum(1 for i in c.injections if i["kind"] in ("profile_load", "command_build") and i["refused"])
        tried = sum(1 for i in c.injections if i["kind"] in ("profile_load", "command_build"))
        if refused != expected["refusals"] or tried != expected["refusals"]:
            problems.append(f"refusals {refused}/{tried} != {expected['refusals']}")
    injected = {i["kind"] for i in c.injections}
    for kind in expected.get("injections", []):
        if kind not in injected:
            problems.append(f"injection_missing:{kind}")


def artifacts(case: Path) -> dict:
    return {str(path.relative_to(case)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(case.rglob("*")) if path.is_file() and "judge" not in path.parts
            and not path.name.endswith(("-wal", "-shm"))}


def judge_vendor_case(case: Path, root: Path, *, use_replay: bool = False) -> dict:
    c = VendorCase(case, root, use_replay=use_replay)
    problems: list[str] = []
    vendor_counts = check_vendor(c, problems)
    check_vendor_expected(c, problems)
    counts = {key: vendor_counts.get(key, 0) for key in COUNTS}
    unsafe = any(counts.values())
    classification = "unsafe_or_incorrect" if unsafe else ("unexpected" if problems else "as_expected")
    stalls = [i for i in c.injections if i["kind"] == "host_stall"]
    if stalls and not unsafe:
        classification = "void"
        problems.append(f"host_stall:{max(i['seconds'] for i in stalls)}s")
    return {"schema_version": "0.1.0", "scenario": c.scenario["scenario"], "seed": c.scenario["seed"], "layer": "S3",
            "source_sha": c.scenario.get("source_sha"), "mode": "replay" if use_replay else "online",
            "classification": classification, "passed": not problems, "counts": counts,
            "false_success_reports": counts["false_success"], "problems": problems,
            "expected": c.scenario.get("expected", {}), "executions": len(c.truth["executions"]),
            "commands": dict(Counter(cmd["method"] for cmd in c.truth["commands"])),
            "missions": {m: row["status"] for m, row in c.missions.items()},
            "artifacts": {} if use_replay else artifacts(case),
            "judged_at": datetime.now().astimezone().isoformat()}


def judge_case(case: Path, root: Path, *, use_replay: bool = False) -> dict:
    scenario = json.loads((case / "input/scenario.json").read_text(encoding="utf-8"))
    if scenario.get("layer") == "S3":
        return judge_vendor_case(case, root, use_replay=use_replay)
    return judge_road_case(case, root, use_replay=use_replay)


class RoadS1Case(judge_p4.S1Case):
    """A PX4 SITL road case: the P4 S1 records with the P5 S1 catalogs. / PX4 SITL 道路用例：换成 P5 S1 目录的 P4 S1 记录。"""

    catalog_path, workflows_path, members_path, business_path = S1_CATALOG, S1_WORKFLOWS, S1_MEMBERS, S1_BUSINESS


def judge_s1_case(case: Path, root: Path, *, use_replay: bool = False) -> dict:
    """The M2 flight judge per mission against Gazebo truth, then the P1, P2 and P4 checks and the obstacle history.

    每个任务用 M2 飞行裁判对 Gazebo 真值核对，再做 P1、P2、P4 检查与障碍物历史核对。
    """
    import tempfile

    from drone_agent.eval.judge_m2 import judge_case as judge_flight
    from drone_agent.eval.judge_p2 import mission_cases

    c = RoadS1Case(case, root)
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
    counts = judge_p4.business_counts(c, problems, use_replay=use_replay, flight_false=flight_false)
    counts["false_closure"] += judge_p4.check_world(c, problems)
    result = judge_p4.verdict(c, problems, counts, case, layer="S1", use_replay=use_replay, flight_judges=flights,
                              missions=len(c.requests))
    result["counts"] = {key: result["counts"].get(key, 0) for key in COUNTS}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--layer", choices=["s0", "s1"], default="s0", help="s0 covers the S0 road and S3 cases")
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
