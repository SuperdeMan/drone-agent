"""Independent judge for one P1 S0 case (WP-P1-11): the service's dispatch decisions against the world.

The judge never reuses the service's gate or eligibility code. From the case recordings it rebuilds what each dock
actually sent and which of those reports the service accepted, then re-checks every claim: fresh report from an
active session, lid open with this activity's open action completed, aircraft on the pad, energy ready with enough
charge, environment permitted, no maintenance lock, valid approval, no cancel intent, no airborne conflict, and a
lid that was truly open when the aircraft took off. It checks that flights never overlap or repeat, that nothing is
claimed after a cancel, that every release follows the activity's landing on the pad and happens at most once, that
every cross-project probe was refused without data, and every flight's reported success against the logical truth
(the M2 step-effect checks, online or from the MCAP replay). Counts: wrong dispatch, duplicate dispatch, wrong
release, project escape and false success; the scenario's own expectations come on top.

单个 P1 S0 用例的独立裁判（WP-P1-11）：以世界核对服务的派遣决定。

裁判从不复用服务的闸门或判定代码。它从用例记录重建每个机场实际发送了什么、服务接受了其中哪些报告，然后复核
每次领取：来自有效会话的新鲜报告、舱盖打开且本活动的开盖动作已完成、飞行器在机位、补能就绪且电量足够、环境
许可、无维护锁、审批有效、无取消意图、无空中冲突，以及飞行器起飞时舱盖真实打开。它还核对飞行从不重叠或重复、
取消后没有领取、每次释放都在本活动于机位落地之后且至多一次、每次跨项目探测都被拒且无数据，以及每次飞行报告的
成功是否符合逻辑真值（M2 的逐步骤效果检查，在线或基于 MCAP 回放）。计数：错误派遣、重复派遣、错误释放、项目
越权与错误成功；场景自身期望另外核对。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from drone_agent.contracts import MissionPackage, StepOutcome
from drone_agent.eval.judge_m2 import _replayed, true_effect
from drone_agent.fleet.resources import load_catalog
from drone_agent.mission.registry import Registry
from drone_agent.runtime.ledger import read_log

CATALOG = "configs/sites/p1_campus_v1.yaml"
ADMIN = "harness:p1-admin"
REFUSED = ("service.not_found", "auth.project_denied", "auth.method_not_allowed", "auth.scope_missing",
           "auth.identity_missing", "service.invalid_request", "dispatch.reservation_conflict")


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] \
        if path.is_file() else []


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Case:
    """Everything recorded for one case, loaded once. / 一个用例的全部记录，只加载一次。"""

    def __init__(self, case: Path, root: Path):
        self.case, self.root = case, root
        self.scenario = json.loads((case / "input/scenario.json").read_text(encoding="utf-8"))
        self.views = json.loads((case / "service-export/views.json").read_text(encoding="utf-8"))
        self.ops = json.loads((case / "service-export/operations.json").read_text(encoding="utf-8"))
        self.snapshots = json.loads((case / "service-export/resources.json").read_text(encoding="utf-8"))
        self.ready = json.loads((case / "service-export/ready.json").read_text(encoding="utf-8"))
        self.transcript = _jsonl(case / "world/api.jsonl")
        self.injections = _jsonl(case / "world/injections.jsonl")
        self.dock_log = _jsonl(case / "world/docks.jsonl")
        self.flights = [f for path in sorted((case / "world").glob("*-flights.json"))
                        for f in json.loads(path.read_text(encoding="utf-8"))]
        self.truth: dict[str, list[dict]] = {}
        for path in sorted((case / "world").glob("*-truth.jsonl")):
            rows = _jsonl(path)
            if rows:
                self.truth[rows[0]["robot_id"]] = rows
        self.catalog = load_catalog(root / CATALOG)
        self.bindings = {row["mission_id"]: row for row in self.ops["op_bindings"]}
        self.events = self.ops["op_events"]

    def events_of(self, subject: str, kind: str | None = None) -> list[dict]:
        return [e for e in self.events if e["subject"] == subject and (kind is None or e["kind"] == kind)]

    def accepted_reports(self, dock_id: str, before: datetime) -> list[dict]:
        """Reports of this dock the service accepted before `before`, with what the dock sent. / 服务在此前接受的报告及发送内容。"""
        sends: dict[tuple[str, int], list[tuple[datetime, dict]]] = {}
        for row in self.dock_log:
            if row["dock_id"] == dock_id and row["kind"] == "report":
                sends.setdefault((row["reported_boot"], row["seq"]), []).append((_at(row["at"]), row["sent"]))
        found = []
        for event in self.events_of(f"dock:{dock_id}", "status.accepted"):
            accepted_at = _at(event["created_at"])
            if accepted_at > before:
                continue
            body = json.loads(event["body"]) if isinstance(event["body"], str) else event["body"]
            # A faulty dock may send the same (boot, seq) twice; the accepted one is the last sent before acceptance.
            # 故障机场可能两次发送同一 (boot, seq)；被接受的是接受之前最后发送的那一份。
            earlier = [sent for at, sent in sends.get((body["boot_id"], body["seq"]), []) if at <= accepted_at]
            found.append({"accepted_at": accepted_at, "boot_id": body["boot_id"], "seq": body["seq"],
                          "sent": earlier[-1] if earlier else None})
        return found


def _body(event: dict) -> dict:
    return json.loads(event["body"]) if isinstance(event["body"], str) else event["body"]


def check_claims(c: Case, problems: list[str]) -> int:
    """Re-derive every claim from the dock's accepted reports and the world truth. / 由机场已接受报告与世界真值复核每次领取。"""
    wrong = 0
    policy = c.catalog.policy
    cancels = {row["mission_id"]: _at(row["requested_at"]) for row in c.ops["op_cancellations"]}
    actions = {(row["activity_key"], row["kind"]): row for row in c.ops["op_dock_actions"]}
    for claim in c.ops["op_claims"]:
        if claim["state"] != "claimed":
            continue
        mission, version, at = claim["mission_id"], claim["mission_version"], _at(claim["decided_at"])
        binding = c.bindings.get(mission)
        if binding is None:
            problems.append(f"claim_without_binding:{mission}")
            wrong += 1
            continue
        tag, dock_id = f"{mission}:v{version}", binding["dock_id"]
        reasons = []
        reports = c.accepted_reports(dock_id, at)
        latest = reports[-1] if reports else None
        sent = latest["sent"] if latest else None
        if sent is None:
            reasons.append("no_accepted_report")
        else:
            age = (at - _at(sent["observed_at"])).total_seconds()
            if age > policy.freshness_s or age < -policy.future_skew_s:
                reasons.append("report_not_fresh")
            session = [r for r in reports if r["boot_id"] == latest["boot_id"]]
            if len(session) < policy.reconcile_reports:
                reasons.append("session_reconciling")
            package = MissionPackage.model_validate_json(
                (c.case / "robots" / binding["robot_id"] / "inbox/history" / f"{mission}-v{version}.json").read_bytes()) \
                if (c.case / "robots" / binding["robot_id"] / "inbox/history" / f"{mission}-v{version}.json").is_file() \
                else None
            need = (package.energy_budget.max_consumption_fraction + package.energy_budget.reserve_fraction) \
                if package else 1.0
            checks = {"link": sent["link"] == "online", "lid_open": sent["lid"] == "open",
                      "present": sent["aircraft"] == "present", "energy_ready": sent["energy"]["state"] == "ready",
                      "charge": (sent["energy"]["charge_fraction"] or 0.0) >= min(1.0, need),
                      "environment": sent["environment"]["state"] == "permitted"
                      and (sent["environment"]["wind_mps"] or 99.0) <= 8.0,
                      "upkeep": sent["upkeep"] == "normal"}
            reasons += [name for name, ok in checks.items() if not ok]
        locks = [e for e in c.events_of(f"dock:{dock_id}") if _at(e["created_at"]) <= at and (
            (e["kind"] == "status.accepted" and _body(e).get("locked")) or e["kind"] in ("lock.set", "lock.released"))]
        if locks and locks[-1]["kind"] != "lock.released":
            reasons.append("maintenance_lock_active")
        opened = actions.get((f"mission:{mission}:v{version}", "open_lid"))
        if opened is None or opened["state"] != "completed" or not opened["result_at"] or _at(opened["result_at"]) > at:
            reasons.append("lid_open_not_confirmed")
        view = c.views.get(mission, {})
        record = next((v for v in view.get("versions", []) if v["version"] == version), {})
        approval = record.get("approval") or {}
        if not approval.get("expires_at") or _at(approval["expires_at"]) <= at:
            reasons.append("approval_invalid")
        if mission in cancels and cancels[mission] <= at:
            reasons.append("claimed_after_cancel")
        conflict = [i for i in c.injections if i["kind"] == "robot_status" and i["robot_id"] == binding["robot_id"]
                    and _at(i["at"]) <= at]
        if conflict and conflict[-1]["phase"] == "airborne":
            reasons.append("robot_reported_airborne")
        if reasons:
            wrong += 1
            problems.append(f"wrong_dispatch:{tag}:{','.join(reasons)}")
    # Physical sanity: the lid was truly open whenever an aircraft took off. / 物理一致性：起飞时舱盖确实打开。
    for flight in c.flights:
        dock_id = c.catalog.robots[flight["robot_id"]].dock_id
        before = [row for row in c.dock_log if row["dock_id"] == dock_id and row["at"] <= flight["started_at"]]
        if not before or before[-1]["lid"] != "open":
            wrong += 1
            problems.append(f"takeoff_without_open_lid:{flight['mission_id']}:v{flight['version']}")
    return wrong


def check_dispatch_counts(c: Case, problems: list[str]) -> int:
    """No overlapping flights per aircraft, no repeated activity, no flight without a claim. / 无重叠、无重复、无未领取飞行。"""
    duplicate = 0
    claimed = {(row["mission_id"], row["mission_version"]) for row in c.ops["op_claims"] if row["state"] == "claimed"}
    seen = Counter((f["mission_id"], f["version"]) for f in c.flights)
    for key, count in seen.items():
        if count > 1:
            duplicate += count - 1
            problems.append(f"repeated_flight:{key[0]}:v{key[1]}")
        if key not in claimed:
            duplicate += 1
            problems.append(f"flight_without_claim:{key[0]}:v{key[1]}")
    by_robot: dict[str, list[dict]] = {}
    for flight in c.flights:
        by_robot.setdefault(flight["robot_id"], []).append(flight)
    for robot, flights in by_robot.items():
        ordered = sorted(flights, key=lambda f: f["started_at"])
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if earlier["ended_at"] is None or later["started_at"] < earlier["ended_at"]:
                duplicate += 1
                problems.append(f"overlapping_flights:{robot}")
    cancels = {row["mission_id"]: _at(row["requested_at"]) for row in c.ops["op_cancellations"]}
    for row in c.ops["op_claims"]:
        if row["state"] == "claimed" and row["mission_id"] in cancels and _at(row["decided_at"]) >= cancels[row["mission_id"]]:
            duplicate += 1
            problems.append(f"claim_after_cancel:{row['mission_id']}")
    return duplicate


def check_releases(c: Case, problems: list[str]) -> int:
    """Releases follow a landing on the pad, unclaimed ones never flew, and none repeats. / 释放在落地后、未领取的从未飞、不重复。"""
    wrong = 0
    claims = {(row["mission_id"], row["mission_version"]): _at(row["decided_at"]) for row in c.ops["op_claims"]
              if row["state"] == "claimed"}
    after_claim: Counter = Counter()
    for event in c.events:
        if event["kind"] != "reservation.released":
            continue
        body, at = _body(event), _at(event["created_at"])
        _, mission, version = event["subject"].split(":")
        version = int(version[1:])
        binding = c.bindings.get(mission)
        flight = next((f for f in c.flights if f["mission_id"] == mission and f["version"] == version), None)
        claimed_at = claims.get((mission, version))
        if claimed_at is not None and at >= claimed_at:
            after_claim[event["subject"]] += 1
        if body["reason"] != "reconciled":
            # A hold released before any claim (soft expiry, decline, void) may be taken again later; after a
            # claim only reconciliation may release it. / 领取前释放的持有可以再取；领取后只有对账能释放。
            if claimed_at is not None and at >= claimed_at:
                wrong += 1
                problems.append(f"unreconciled_release_after_claim:{mission}:v{version}")
            continue
        if flight is None:
            continue  # the robot refused the package; presence was still required / 机器人拒收；仍要求在位
        if flight["ended_at"] is None or _at(flight["ended_at"]) > at:
            wrong += 1
            problems.append(f"released_before_flight_ended:{mission}:v{version}")
        samples = [row for row in c.truth.get(binding["robot_id"], []) if _at(row["timestamp"]) <= at]
        if not samples or samples[-1]["in_air"] or samples[-1]["position"][2] > 0.3:
            wrong += 1
            problems.append(f"released_while_not_on_pad:{mission}:v{version}")
    for subject, count in after_claim.items():
        if count > 1:
            wrong += 1
            problems.append(f"released_more_than_once:{subject}")
    return wrong


def check_probes(c: Case, problems: list[str]) -> int:
    """Every probe is refused and returns no data; listings never show other projects. / 每次探测被拒且无数据。"""
    escapes = 0
    for row in c.transcript:
        if not row["probe"]:
            continue
        if row["method"].startswith("docks.") and row["ok"]:
            refused = (row.get("result") or {}).get("accepted") is False
        else:
            refused = not row["ok"] and row["code"] in REFUSED and row["result_sha256"] is None
        if not refused:
            escapes += 1
            problems.append(f"probe_not_refused:{row['actor']}:{row['method']}")
    for item in c.injections:
        if item["kind"] == "operator_list":
            others = [m for m in item["missions"] if (c.bindings.get(m) or {}).get("project_id") != "campus_ops"]
            if others:
                escapes += 1
                problems.append("listing_shows_other_projects")
    return escapes


def check_flights(c: Case, problems: list[str], *, use_replay: bool) -> int:
    """Onboard successes against the logical truth, and the report against both. / 机载成功对逻辑真值，报告对二者。"""
    false_success = 0
    signer = c.ready.get("signer_key_id")
    truly: dict[str, set[str]] = {}
    onboard: dict[tuple[str, int], dict[str, StepOutcome]] = {}
    for flight in c.flights:
        mission, version, robot = flight["mission_id"], flight["version"], flight["robot_id"]
        directory = c.case / "robots" / robot / "aircraft" / mission / f"v{version}"
        package_file = c.case / "robots" / robot / "inbox/history" / f"{mission}-v{version}.json"
        if not package_file.is_file() or not (directory / "executive.jsonl").is_file():
            problems.append(f"flight_records_missing:{mission}:v{version}")
            continue
        package = MissionPackage.model_validate_json(package_file.read_bytes())
        registry = Registry(c.root, scene=c.root / c.catalog.sites[c.catalog.robots[robot].site_id].scene)
        guardian = read_log(directory / "guardian.jsonl")
        executive = _replayed(directory / "executive.mcap") if use_replay else read_log(directory / "executive.jsonl")
        verified = [r for r in guardian if r["kind"] == "package_verified"]
        if not verified or verified[0]["data"]["signer_key_id"] != signer:
            problems.append(f"package_not_verified_with_service_key:{mission}:v{version}")
        outcomes = {}
        for row in executive:
            if row["kind"] == "step_outcome":
                outcome = StepOutcome.model_validate(row["data"]["outcome"])
                outcomes[outcome.step_id] = outcome
        onboard[(mission, version)] = outcomes
        truth = c.truth.get(robot, [])
        for node in package.nodes:
            outcome = outcomes.get(node.task_id)
            if outcome is None or not outcome.counts_as_completed:
                continue
            if true_effect(node, directory, executive, truth, registry):
                if "asset_id" in node.params:
                    truly.setdefault(mission, set()).add(node.params["asset_id"])
            else:
                false_success += 1
                problems.append(f"untrue_effect:{mission}:v{version}:{node.task_id}")
        record = next((v for v in c.views.get(mission, {}).get("versions", []) if v["version"] == version), {})
        provenance = record.get("provenance") or {}
        if provenance.get("execution_backend") != "logical_sim":
            problems.append(f"flight_not_labelled_logical:{mission}:v{version}")
        if any(image.get("source") != "test_fixture" for image in provenance.get("imagery", [])):
            problems.append(f"imagery_not_labelled_fixture:{mission}:v{version}")
    for mission, view in c.views.items():
        report = view.get("report") if isinstance(view, dict) else None
        if not report:
            continue
        for row in report["rows"]:
            outcome = onboard.get((mission, row["mission_version"]), {}).get(row["step_id"])
            if row["column"] == "completed" and (outcome is None or not outcome.counts_as_completed):
                false_success += 1
                problems.append(f"report_completed_without_onboard_success:{mission}:{row['step_id']}")
        for target, column in report["targets"].items():
            if column == "completed" and target not in truly.get(mission, set()):
                false_success += 1
                problems.append(f"report_completed_without_true_inspection:{mission}:{target}")
    return false_success


def check_expected(c: Case, problems: list[str]) -> None:
    expected = c.scenario.get("expected", {})
    probe_missions = {row["mission_id"] for row in c.transcript if row["probe"] and row["mission_id"]}
    missions = [m for m in c.views if m not in probe_missions]
    if c.scenario.get("harness_error"):
        problems.append(f"harness_error:{c.scenario['harness_error']}")
    if "flights" in expected and len(c.flights) != expected["flights"]:
        problems.append(f"flights {len(c.flights)} != {expected['flights']}")
    if "statuses" in expected:
        statuses = sorted(c.views[m]["mission"]["status"] for m in missions if "mission" in c.views[m])
        if statuses != sorted(expected["statuses"]):
            problems.append(f"statuses {statuses} != {sorted(expected['statuses'])}")
    seen = {reason for row in c.ops["op_decisions"] for reason in json.loads(row["body"])["reasons"]}
    for reason in expected.get("reasons", []):
        if reason not in seen:
            problems.append(f"reason_never_recorded:{reason}")
    rejected = {_body(e)["reason"] for e in c.events if e["kind"] == "status.rejected"}
    for reason in expected.get("rejections", []):
        if reason not in rejected:
            problems.append(f"rejection_never_recorded:{reason}")
    if expected.get("uncertain") and not any(e["kind"] == "reservation.uncertain" for e in c.events):
        problems.append("no_uncertain_hold")
    refused = [r for r in c.transcript if r["method"] == "approve" and not r["probe"] and not r["ok"]]
    if "approvals_refused" in expected and len(refused) != expected["approvals_refused"]:
        problems.append(f"approvals_refused {len(refused)} != {expected['approvals_refused']}")
    for kind, state in expected.get("actions", {}).items():
        states = {row["state"] for row in c.ops["op_dock_actions"] if row["kind"] == kind}
        if state not in states or (state == "acked" and "completed" in states):
            problems.append(f"action_{kind}_states {sorted(states)} lack {state}")
    if expected.get("lock_released_by_admin") and not any(
            e["kind"] == "lock.released" and e["actor"] == ADMIN for e in c.events):
        problems.append("lock_not_released_by_admin")
    if expected.get("relayed_cancel") and not any(e["kind"] == "cancel.relayed" for e in c.events):
        problems.append("cancel_not_relayed")
    if expected.get("session_new") and not any(e["kind"] == "session.new" for e in c.events):
        problems.append("no_new_session")
    ignored = sum(1 for e in c.events if e["kind"] == "action.ack_ignored")
    if "ack_ignored" in expected and ignored < expected["ack_ignored"]:
        problems.append(f"ack_ignored {ignored} < {expected['ack_ignored']}")
    if expected.get("forged_catalog_refused") and not any(
            i["kind"] == "forged_catalog" and i["accepted"] is False for i in c.injections):
        problems.append("forged_catalog_not_refused")
    if expected.get("resubmit_same") and not any(
            i["kind"] == "resubmitted" and i["same"] is True for i in c.injections):
        problems.append("resubmission_created_another_mission")


def judge_case(case: Path, root: Path, *, use_replay: bool = False) -> dict:
    c = Case(case, root)
    problems: list[str] = []
    counts = {"wrong_dispatch": check_claims(c, problems), "duplicate_dispatch": check_dispatch_counts(c, problems),
              "wrong_release": check_releases(c, problems), "project_escape": check_probes(c, problems),
              "false_success": check_flights(c, problems, use_replay=use_replay)}
    unsafe = any(counts.values())
    check_expected(c, problems)
    classification = "unsafe_or_incorrect" if unsafe else ("unexpected" if problems else "as_expected")
    stalls = [i for i in c.injections if i["kind"] == "host_stall"]
    if stalls and not unsafe:
        # Timing expectations do not hold across a host stall; safety counts still would (D045).
        # 主机停顿使时间相关期望失效；安全计数仍然有效（D045）。
        classification = "void"
        problems.append(f"host_stall:{max(i['seconds'] for i in stalls)}s")
    return {"schema_version": "0.1.0", "scenario": c.scenario["scenario"], "seed": c.scenario["seed"],
            "layer": "S0", "source_sha": c.scenario.get("source_sha"), "mode": "replay" if use_replay else "online",
            "classification": classification, "passed": not problems, "counts": counts,
            "false_success_reports": counts["false_success"], "problems": problems,
            "expected": c.scenario.get("expected", {}), "flights": len(c.flights),
            "claims": sum(1 for r in c.ops["op_claims"] if r["state"] == "claimed"),
            "voids": sum(1 for r in c.ops["op_claims"] if r["state"] == "void"),
            "probes": sum(1 for r in c.transcript if r["probe"]),
            "artifacts": {} if use_replay else {
                str(path.relative_to(case)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(case.rglob("*")) if path.is_file() and "judge" not in path.parts
                and path.suffix != ".sqlite3-wal" and not path.name.endswith(("-wal", "-shm"))},
            "judged_at": (datetime.now().astimezone() + timedelta(0)).isoformat()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    result = judge_case(args.case, args.root, use_replay=args.replay)
    print(json.dumps({k: v for k, v in result.items() if k != "artifacts"}, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
