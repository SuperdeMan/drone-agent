"""Independent judge for one P4 case (D063 §12): the business ledger against the world and the people's actions.

The judge never reuses the business engine, its store or the service. From the exported tables, the API transcript,
the injections, the appearance history and the aircraft's own records it re-derives that each defect cluster had at
most one open finding at a time and each finding at most one order; that every decided finding, every order and
every closure rests on a first-party reviewer of that project; that each closed order passed `reinspection-v1`
recomputed here from the round's feedback, the reinspection run's verified evidence and its capture time, the
novelty of that acquisition and its media, the reinspection analysis and the round's review; that analyses read
only verified evidence and never changed a mission's report; that every job ended with a verdict and a source, or a
refusal with a reason; that nothing was opened or closed for a run after its cancel; and that probes were refused
without data. The P1 dispatch, release and flight checks and the P2 workflow checks run on the same case.

单个 P4 用例的独立裁判（D063 §12）：以世界与人的操作核对业务账本。

裁判从不复用业务引擎、其存储或服务。它从导出表、API 记录、注入、外观历史与飞行器自身记录重新推导：每个缺陷聚合键
任一时刻至多一个未结发现，每个发现至多一张工单；每个已决定的发现、每张工单与每次关单都建立在该项目第一方 reviewer
之上；每张已关单的工单都通过在这里独立复算的 `reinspection-v1`（依据本轮反馈、复检运行已证实的证据及其采集时刻、
该采集与媒体的新颖性、复检分析与本轮复核）；分析只读取已证实的证据且从不改变任务报告；每个作业都以带来源的结论或带
原因的拒判结束；运行取消后没有为其开启或关闭任何东西；探测都被拒且不泄漏数据。P1 的派遣、释放与飞行检查以及 P2 的
工作流检查在同一用例上运行。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from drone_agent.eval.judge_p1 import check_claims, check_dispatch_counts, check_releases
from drone_agent.eval.judge_p2 import REFUSED as P2_REFUSED
from drone_agent.eval.judge_p2 import Case as P2Case
from drone_agent.eval.judge_p2 import S1Case as P2S1Case
from drone_agent.eval.judge_p2 import check_cancel, check_dispatch, check_success, check_triggers, mission_cases
from drone_agent.fleet.business_models import load_catalog

WORKFLOWS = "configs/workflows/p4_campus_v1.yaml"
MEMBERS = "configs/sites/p4_members_s0.yaml"
BUSINESS = "configs/analysis/p4_campus_v1.yaml"
S1_WORKFLOWS = "configs/workflows/p4_s1_v1.yaml"
S1_MEMBERS = "configs/sites/p4_members_s1.yaml"
S1_BUSINESS = "configs/analysis/p4_s1_v1.yaml"
COUNTS = ("duplicate_finding", "duplicate_order", "unreviewed_order", "non_human_decision", "false_closure",
          "unverified_analysis", "verdict_mutation", "post_cancel_effect", "lost_job", "default_verdict",
          "project_escape", "duplicate_dispatch", "post_cancel_dispatch", "false_success", "wrong_dispatch",
          "wrong_release")
REFUSED = (*P2_REFUSED, "reuse.modality_mismatch", "reuse.source_unknown", "reuse.resolution", "reuse.stale",
           "reuse.analyzer_not_allowed", "analysis.not_verified", "auth.scope_missing")
VERDICTS = ("suspected", "normal")
SOURCES = ("live_model", "recorded_model", "scripted", "deterministic")


def _at(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


class BusinessTables:
    """The business tables and the appearance history of a case, on top of its P2 records.

    用例的业务表与外观历史，叠加在其 P2 记录之上。
    """

    business_path = BUSINESS

    def _business_tables(self, case: Path, root: Path) -> None:
        self.bz = json.loads((case / "service-export/business.json").read_text(encoding="utf-8"))
        appearance = case / "service-export/appearance.json"
        self.appearance = json.loads(appearance.read_text(encoding="utf-8")) if appearance.is_file() else {}
        self.business_catalog = load_catalog(root / self.business_path)
        self.jobs = {row["job_id"]: {**row, "inputs": _json(row["inputs"]), "result": _json(row["result"])}
                     for row in self.bz["bz_jobs"]}
        self.findings = {row["finding_id"]: {**row, "body": _json(row["body"])} for row in self.bz["bz_findings"]}
        self.reviews = {(row["subject_kind"], row["subject_id"]): row for row in self.bz["bz_reviews"]}
        self.orders = {row["order_id"]: {**row, "reinspection": _json(row["reinspection"]),
                                         "closure": _json(row["closure"])} for row in self.bz["bz_orders"]}
        self.rounds = [{**row, "feedback": _json(row["feedback"]), "conclusion": _json(row["conclusion"])}
                       for row in self.bz["bz_rounds"]]
        self.evidence = {row["evidence_id"]: {**row, "body": _json(row["body"])} for row in self.bz["evidence"]}
        self.bindings = {row["mission_id"]: row for row in self.bz["op_bindings"]}

    def reviewer(self, principal: str, project: str) -> bool:
        return principal.startswith("harness:") and "reviewer" in self.roles(principal, project)

    def run_of(self, requested_by: str) -> dict | None:
        return self.runs.get(requested_by.split(":", 1)[1]) if requested_by.startswith("workflow:") else None


class Case(BusinessTables, P2Case):
    """An S0 case: the P2 records plus the business tables. / S0 用例：P2 记录加上业务表。"""

    workflows_path, members_path, business_path = WORKFLOWS, MEMBERS, BUSINESS

    def __init__(self, case: Path, root: Path):
        super().__init__(case, root)
        self._business_tables(case, root)


class S1Case(BusinessTables, P2S1Case):
    """A PX4 SITL case: the P2 S1 records plus the business tables. / PX4 SITL 用例：P2 S1 记录加上业务表。"""

    workflows_path, members_path, business_path = S1_WORKFLOWS, S1_MEMBERS, S1_BUSINESS

    def __init__(self, case: Path, root: Path):
        super().__init__(case, root)
        self._business_tables(case, root)


def check_findings(c: Case, problems: list[str]) -> tuple[int, int]:
    """(duplicate findings, non-human decisions). / （重复发现，非人工决定）。"""
    duplicate = decisions = 0
    end = datetime.max.replace(tzinfo=None)
    clusters: dict[str, list[tuple]] = {}
    for finding in c.findings.values():
        start, closed = _at(finding["created_at"]), _at(finding["closed_at"])
        clusters.setdefault(finding["cluster_key"], []).append(
            (start.replace(tzinfo=None), closed.replace(tzinfo=None) if closed else end, finding["finding_id"]))
        review = c.reviews.get(("finding", finding["finding_id"]))
        if finding["state"] != "candidate":
            if review is None or not c.reviewer(review["reviewer"], finding["project_id"]):
                decisions += 1
                problems.append(f"finding_{finding['state']}_without_reviewer:{finding['finding_id']}")
            elif (review["decision"] == "confirmed") != (finding["state"] in ("confirmed", "resolved")):
                decisions += 1
                problems.append(f"finding_state_differs_from_review:{finding['finding_id']}")
        if finding["state"] == "resolved":
            order = next((o for o in c.orders.values() if o["finding_id"] == finding["finding_id"]), None)
            if order is None or order["state"] != "closed":
                decisions += 1
                problems.append(f"finding_resolved_without_closed_order:{finding['finding_id']}")
    for cluster, spans in clusters.items():
        spans.sort()
        for (_, left_end, left), (right_start, _, right) in zip(spans, spans[1:], strict=False):
            if right_start < left_end:
                duplicate += 1
                problems.append(f"two_open_findings:{cluster}:{left}:{right}")
    for review in c.bz["bz_reviews"]:
        if not c.reviewer(review["reviewer"], review["project_id"]):
            decisions += 1
            problems.append(f"review_by_non_reviewer:{review['review_id']}")
    for job in c.jobs.values():
        run = c.run_of(job["requested_by"])
        cancelled = run is not None and run["cancel"] is not None
        if job["verdict"] == "suspected" and job["purpose"] != "reinspection" and not job["finding_id"] \
                and not cancelled:
            duplicate += 1
            problems.append(f"suspected_job_without_finding:{job['job_id']}")
    return duplicate, decisions


def check_orders(c: Case, problems: list[str]) -> tuple[int, int]:
    """(duplicate orders, orders without a reviewer's confirmation). / （重复工单，没有复核人确认的工单）。"""
    duplicate = unreviewed = 0
    for finding_id, count in Counter(o["finding_id"] for o in c.orders.values()).items():
        if count > 1:
            duplicate += count - 1
            problems.append(f"two_orders_for_one_finding:{finding_id}")
    clusters = Counter((c.findings[o["finding_id"]]["cluster_key"], o["state"] != "closed") for o in c.orders.values()
                       if o["finding_id"] in c.findings)
    for (cluster, open_), count in clusters.items():
        if open_ and count > 1:
            duplicate += count - 1
            problems.append(f"two_open_orders_for_one_cluster:{cluster}")
    for order in c.orders.values():
        review = c.reviews.get(("finding", order["finding_id"]))
        if review is None or review["decision"] != "confirmed" or review["review_id"] != order["review_id"] \
                or not c.reviewer(review["reviewer"], order["project_id"]) \
                or _at(review["created_at"]) > _at(order["created_at"]):
            unreviewed += 1
            problems.append(f"order_without_prior_reviewer_confirmation:{order['order_id']}")
    return duplicate, unreviewed


def asset_captures(c: Case, project: str, asset_key: str) -> list[dict]:
    """Every acquisition of an asset key, recomputed from bindings and evidence subjects. / 资产键的全部采集。"""
    found = []
    for evidence in c.evidence.values():
        binding = c.bindings.get(evidence["mission_id"])
        subjects = evidence["body"].get("subject_ids") or []
        if binding is None or binding["project_id"] != project or len(subjects) != 1:
            continue
        if f"{project}/{binding['site_id']}/{subjects[0]}" == asset_key or f"{project}/*/{subjects[0]}" == asset_key:
            found.append(evidence)
    return found


def recompute_round(c: Case, order: dict, round_row: dict) -> tuple[str, list[str]]:
    """`reinspection-v1` recomputed from the exported records. / 依据导出记录复算 `reinspection-v1`。"""
    reasons = []
    feedback = round_row["feedback"] or {}
    reported = _at(feedback.get("reported_at"))
    run_id = round_row["reinspection_run"]
    nodes = c.nodes.get(run_id, {})
    inspection = next((_json(n["result"]) for n in nodes.values()
                       if n["activity"] == "await_mission" and n["state"] == "completed"), None)
    if reported is None or inspection is None:
        return "unknown", ["evidence_missing"]
    if c.verdicts.get(inspection["evidence_id"]) != "verified":
        return "unknown", ["not_verified"]
    evidence = c.evidence.get(inspection["evidence_id"])
    captured = _at(((evidence or {}).get("body") or {}).get("time_window", {}).get("timestamp"))
    if captured is None:
        return "unknown", ["capture_time_unknown"]
    if captured <= reported:
        return "unknown", ["before_feedback"]
    run_missions = {r["mission_id"] for r in c.run_missions(run_id)}
    if evidence["mission_id"] not in run_missions:
        return "unknown", ["not_new_acquisition"]
    earlier = [e for e in asset_captures(c, order["project_id"], order["asset_key"])
               if e["mission_id"] not in run_missions]
    if inspection["evidence_id"] in {e["evidence_id"] for e in earlier}:
        return "unknown", ["not_new_acquisition"]
    if evidence["sha256"] in {e["sha256"] for e in earlier}:
        return "unknown", ["replayed_media"]
    job = next((j for j in c.jobs.values() if j["requested_by"] == f"workflow:{run_id}"
                and j["purpose"] == "reinspection" and j["evidence_id"] == inspection["evidence_id"]), None)
    if job is None or job["verdict"] not in VERDICTS:
        return "unknown", ["analysis_refused"]
    if job["source"] not in c.business_catalog.closure.allowed_sources:
        return "unknown", ["source_not_allowed"]
    if job["verdict"] == "suspected":
        return "failed", ["still_anomalous"]
    review = c.reviews.get(("round", f"{order['order_id']}:r{round_row['round']}"))
    if review is None:
        return "unknown", ["review_missing"]
    if not c.reviewer(review["reviewer"], order["project_id"]):
        reasons.append("review_by_non_reviewer")
        return "unknown", reasons
    if review["decision"] != "confirmed":
        return "failed", ["review_dismissed"]
    return "passed", []


def check_closure(c: Case, problems: list[str]) -> int:
    """Every closed order passed `reinspection-v1` recomputed here; no round passed without closing its order.

    每张已关单的工单都通过此处复算的 `reinspection-v1`；没有未关单的通过轮次。
    """
    false = 0
    for order in c.orders.values():
        rounds = sorted((r for r in c.rounds if r["order_id"] == order["order_id"]), key=lambda r: r["round"])
        passed = [r for r in rounds if r["state"] == "passed"]
        if order["state"] == "closed":
            if len(passed) != 1 or passed[0]["round"] != order["round"]:
                false += 1
                problems.append(f"closed_order_without_its_passed_round:{order['order_id']}")
                continue
            status, reasons = recompute_round(c, order, passed[0])
            if status != "passed":
                false += 1
                problems.append(f"false_closure:{order['order_id']}:{','.join(reasons)}")
            if c.findings.get(order["finding_id"], {}).get("state") != "resolved":
                false += 1
                problems.append(f"closed_order_with_open_finding:{order['order_id']}")
        elif passed:
            false += 1
            problems.append(f"passed_round_without_closure:{order['order_id']}")
        for row in rounds:
            if row["state"] in ("failed", "unknown", "passed"):
                status, _ = recompute_round(c, order, row)
                if status == "passed" and row["state"] != "passed" and row["conclusion"] and \
                        "reinspection.run_ended" not in row["conclusion"].get("reasons", []):
                    # Stricter than necessary is not a false closure; record it for the expectations only.
                    # 比必要更严格不是错误关单；只记录给期望检查。
                    problems.append(f"round_refused_although_it_passes:{order['order_id']}:r{row['round']}")
    return false


def check_world(c: Case, problems: list[str]) -> int:
    """S1: every closed order's reinspection capture was taken while the Gazebo world had no damage patch, by the
    harness's own record of placing and removing it.

    S1：按编排自身放置与移除贴片的记录，每张已关单工单的复检采集都发生在 Gazebo 世界没有损伤贴片之时。
    """
    history = sorted((_at(h["at"]), h["state"]) for h in c.appearance.get("history", []) if h.get("at"))
    false = 0
    for order in c.orders.values():
        if order["state"] != "closed":
            continue
        passed = next((r for r in c.rounds if r["order_id"] == order["order_id"] and r["state"] == "passed"), None)
        nodes = c.nodes.get(passed["reinspection_run"], {}) if passed else {}
        inspection = next((_json(n["result"]) for n in nodes.values()
                           if n["activity"] == "await_mission" and n["state"] == "completed"), None)
        evidence = c.evidence.get((inspection or {}).get("evidence_id"), {})
        captured = _at(((evidence.get("body") or {}).get("time_window") or {}).get("timestamp"))
        state = None
        for at, value in history:
            if captured is not None and at <= captured:
                state = value
        if state != "normal":
            false += 1
            problems.append(f"closed_while_the_world_was_damaged:{order['order_id']}:{state}")
    return false


def check_analyses(c: Case, problems: list[str]) -> tuple[int, int, int, int]:
    """(unverified analyses, verdict mutations, lost jobs, default verdicts). / （未证实分析、判定改写、丢失作业、缺省结论）。"""
    unverified = mutated = lost = defaulted = 0
    for job in c.jobs.values():
        evidence = job["inputs"]["evidence"]
        if c.verdicts.get(evidence["evidence_id"]) != "verified" or \
                c.evidence.get(evidence["evidence_id"], {}).get("sha256") != job["media_sha256"]:
            unverified += 1
            problems.append(f"analysis_of_unverified_evidence:{job['job_id']}")
        binding = c.bindings.get(evidence["mission_id"])
        if binding is None or binding["project_id"] != job["project_id"]:
            unverified += 1
            problems.append(f"analysis_outside_the_evidence_project:{job['job_id']}")
        if job["state"] not in ("completed", "refused", "cancelled"):
            lost += 1
            problems.append(f"job_not_settled:{job['job_id']}:{job['state']}")
            continue
        result = job["result"] or {}
        if job["state"] == "completed" and (job["verdict"] not in VERDICTS or job["source"] not in SOURCES):
            defaulted += 1
            problems.append(f"completed_job_without_verdict_or_source:{job['job_id']}")
        if job["state"] == "refused" and (job["verdict"] != "refused" or not result.get("reasons")):
            defaulted += 1
            problems.append(f"refusal_without_reason:{job['job_id']}")
        if job["inputs"]["analyzer_kind"] == "model" and job["state"] == "completed" and \
                job["source"] == "deterministic":
            defaulted += 1
            problems.append(f"model_job_reported_deterministic:{job['job_id']}")
        version = next((v for v in c.bz["versions"] if v["mission_id"] == evidence["mission_id"]
                        and v["version"] == evidence["mission_version"]), None) if "versions" in c.bz else None
        if version is not None:
            sealed = (_json(version["decision"]) or {}).get("run_provenance", {})
            if f"analysis:{job['job_id']}" not in sealed and job["state"] != "cancelled":
                defaulted += 1
                problems.append(f"analysis_without_sealed_source:{job['job_id']}")
    reports = {row["mission_id"]: _json(row["body"]) for row in c.bz["reports"]}
    before = {i["mission_id"]: i["report"] for i in c.injections if i["kind"] == "report_before_analysis"}
    for mission_id, report in before.items():
        after = reports.get(mission_id) or {}
        if (report or {}).get("targets") != after.get("targets") or \
                (report or {}).get("columns") != after.get("columns"):
            mutated += 1
            problems.append(f"report_changed_by_analysis:{mission_id}")
    return unverified, mutated, lost, defaulted


def check_post_cancel(c: Case, problems: list[str]) -> int:
    """Nothing opened or closed for a run after its cancel committed. / 运行取消提交后没有为其开启或关闭任何东西。"""
    effects = 0
    for run_id, run in c.runs.items():
        at = c.cancel_time(run_id)
        if at is None:
            continue
        for finding in c.findings.values():
            first = c.jobs.get(finding["first_job"])
            if first and first["requested_by"] == f"workflow:{run_id}" and _at(finding["created_at"]) > at:
                effects += 1
                problems.append(f"finding_after_cancel:{finding['finding_id']}")
        for order in c.orders.values():
            if order["idempotency_key"].startswith(f"wf:{run_id}:") and _at(order["created_at"]) > at:
                effects += 1
                problems.append(f"order_after_cancel:{order['order_id']}")
        for row in c.rounds:
            if row["reinspection_run"] == run_id and row["state"] == "passed":
                effects += 1
                problems.append(f"cancelled_reinspection_closed:{row['order_id']}")
    return effects


def check_probes(c: Case, problems: list[str]) -> int:
    escapes = 0
    for row in c.transcript:
        if not row["probe"]:
            continue
        refused = not row["ok"] and row["code"] in REFUSED and row["result_sha256"] is None
        if not refused:
            escapes += 1
            problems.append(f"probe_not_refused:{row['actor']}:{row['method']}:{row.get('code')}")
    for finding in c.findings.values():
        project = finding["asset_key"].split("/", 1)[0]
        if project != finding["project_id"]:
            escapes += 1
            problems.append(f"finding_outside_its_project:{finding['finding_id']}")
    return escapes


def check_expected(c: Case, problems: list[str]) -> None:
    expected = c.scenario.get("expected", {})
    if c.scenario.get("harness_error"):
        problems.append(f"harness_error:{c.scenario['harness_error']}")
    if "flights" in expected and len(c.flights) != expected["flights"]:
        problems.append(f"flights {len(c.flights)} != {expected['flights']}")
    if "runs" in expected:
        states = dict(Counter(run["state"] for run in c.runs.values()))
        if states != expected["runs"]:
            problems.append(f"runs {states} != {expected['runs']}")
    if "findings" in expected:
        states = dict(Counter(f["state"] for f in c.findings.values()))
        if states != expected["findings"]:
            problems.append(f"findings {states} != {expected['findings']}")
    if "orders" in expected:
        states = dict(Counter(o["state"] for o in c.orders.values()))
        if states != expected["orders"]:
            problems.append(f"orders {states} != {expected['orders']}")
    if "rounds" in expected:
        found = sorted((r["state"], ",".join((r["conclusion"] or {}).get("reasons", []))) for r in c.rounds)
        wanted = sorted((s, r) for s, r in (item.split("|", 1) if "|" in item else (item, "")
                                            for item in expected["rounds"]))
        if found != wanted:
            problems.append(f"rounds {found} != {wanted}")
    if "jobs" in expected:
        states = dict(Counter(j["state"] for j in c.jobs.values()))
        if states != expected["jobs"]:
            problems.append(f"jobs {states} != {expected['jobs']}")
    if "finding_jobs" in expected:
        sizes = sorted(f["jobs"] for f in c.findings.values())
        if sizes != sorted(expected["finding_jobs"]):
            problems.append(f"finding_jobs {sizes} != {sorted(expected['finding_jobs'])}")
    if "refusals" in expected:
        reasons = sorted({(j["result"] or {}).get("reasons", ["?"])[0] for j in c.jobs.values()
                          if j["state"] == "refused"})
        missing = sorted(set(expected["refusals"]) - set(reasons))
        if missing:
            problems.append(f"refusals missing {missing} (got {reasons})")
    if "reviews" in expected and len(c.bz["bz_reviews"]) != expected["reviews"]:
        problems.append(f"reviews {len(c.bz['bz_reviews'])} != {expected['reviews']}")
    if "references" in expected and len(c.bz["bz_references"]) != expected["references"]:
        problems.append(f"references {len(c.bz['bz_references'])} != {expected['references']}")
    if expected.get("probes") and not any(r["probe"] for r in c.transcript):
        problems.append("no_probes_recorded")
    injected = {i["kind"] for i in c.injections}
    for kind in expected.get("injections", []):
        if kind not in injected:
            problems.append(f"injection_missing:{kind}")
    if expected.get("one_concurrent_review"):
        record = next((i for i in c.injections if i["kind"] == "concurrent_reviews"), None)
        if record is None or [record["first"], record["second"]].count(True) != 1:
            problems.append("concurrent_reviews_not_exactly_one")
    if "crash_windows" in expected:
        windows = {i["window"] for i in c.injections if i["kind"] == "crash"}
        if len(windows) != expected["crash_windows"]:
            problems.append(f"crash windows {sorted(windows)}")
    if "max_job_attempts" in expected:
        attempts = max((j["attempts"] for j in c.jobs.values()), default=0)
        if attempts != expected["max_job_attempts"]:
            problems.append(f"max_job_attempts {attempts} != {expected['max_job_attempts']}")
    if expected.get("no_jobs_for_unverified"):
        flat = [r for r in c.runs.values() if r["trigger_event"] == "watch-flat"]
        if any(j["requested_by"] == f"workflow:{r['run_id']}" for r in flat for j in c.jobs.values()):
            problems.append("a_job_was_queued_for_an_unverified_inspection")


def business_counts(c, problems: list[str], *, use_replay: bool, flight_false: int) -> dict:
    """Every count of one case; `flight_false` is the S0 logical-truth check or the S1 flight judges' count.

    一个用例的全部计数；`flight_false` 为 S0 逻辑真值检查或 S1 飞行裁判的计数。
    """
    post_dispatch, _ = check_cancel(c, problems)
    lost_runs, trigger_escapes = check_triggers(c, problems)
    duplicate_finding, decisions = check_findings(c, problems)
    duplicate_order, unreviewed = check_orders(c, problems)
    unverified, mutated, lost_jobs, defaulted = check_analyses(c, problems)
    return {"duplicate_finding": duplicate_finding, "duplicate_order": duplicate_order,
            "unreviewed_order": unreviewed, "non_human_decision": decisions,
            "false_closure": check_closure(c, problems), "unverified_analysis": unverified,
            "verdict_mutation": mutated, "post_cancel_effect": check_post_cancel(c, problems),
            "lost_job": lost_jobs + lost_runs, "default_verdict": defaulted,
            "project_escape": trigger_escapes + check_probes(c, problems),
            "duplicate_dispatch": check_dispatch(c, problems) + check_dispatch_counts(c, problems),
            "post_cancel_dispatch": post_dispatch,
            "false_success": check_success(c, problems, use_replay=use_replay, flight_false=flight_false),
            "wrong_dispatch": check_claims(c, problems), "wrong_release": check_releases(c, problems)}


def artifacts(case: Path) -> dict:
    return {str(path.relative_to(case)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(case.rglob("*")) if path.is_file() and "judge" not in path.parts
            and path.name != "compose.log" and not path.name.endswith(("-wal", "-shm"))}


def verdict(c, problems: list[str], counts: dict, case: Path, *, layer: str, use_replay: bool, **extra) -> dict:
    unsafe = any(counts.values())
    check_expected(c, problems)
    classification = "unsafe_or_incorrect" if unsafe else ("unexpected" if problems else "as_expected")
    stalls = [i for i in c.injections if i["kind"] == "host_stall"]
    if stalls and not unsafe:
        classification = "void"
        problems.append(f"host_stall:{max(i['seconds'] for i in stalls)}s")
    return {"schema_version": "0.1.0", "scenario": c.scenario["scenario"], "seed": c.scenario["seed"],
            "layer": layer, "source_sha": c.scenario.get("source_sha"), "mode": "replay" if use_replay else "online",
            "classification": classification, "passed": not problems, "counts": counts,
            "false_success_reports": counts["false_success"], "problems": problems,
            "expected": c.scenario.get("expected", {}), "flights": len(c.flights), "runs": len(c.runs),
            "findings": len(c.findings), "orders": len(c.orders), "jobs": len(c.jobs),
            "probes": sum(1 for r in c.transcript if r["probe"]), **extra,
            "artifacts": {} if use_replay else artifacts(case),
            "judged_at": (datetime.now().astimezone() + timedelta(0)).isoformat()}


def judge_case(case: Path, root: Path, *, use_replay: bool = False) -> dict:
    from drone_agent.eval.judge_p1 import check_flights

    c = Case(case, root)
    problems: list[str] = []
    flight_false = check_flights(c, problems, use_replay=use_replay)
    counts = business_counts(c, problems, use_replay=use_replay, flight_false=flight_false)
    return verdict(c, problems, counts, case, layer="S0", use_replay=use_replay)


def judge_s1_case(case: Path, root: Path, *, use_replay: bool = False) -> dict:
    """The M2 flight judge per mission against Gazebo truth, then the P1, P2 and P4 checks.

    每个任务用 M2 飞行裁判对 Gazebo 真值核对，再做 P1、P2 与 P4 检查。
    """
    import tempfile

    from drone_agent.eval.judge_m2 import judge_case as judge_flight

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
    counts = business_counts(c, problems, use_replay=use_replay, flight_false=flight_false)
    counts["false_closure"] += check_world(c, problems)
    return verdict(c, problems, counts, case, layer="S1", use_replay=use_replay, flight_judges=flights,
                   missions=len(c.requests))


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
