"""Independent judge for one P2 S0 case (WP-P2-08): the workflow ledger against the world and the people's actions.

The judge never reuses the engine, the workflow store or the service. From the exported tables, the API transcript,
the injections and the aircraft's own records it re-derives that every logical activity produced at most one mission;
that nothing was created, claimed or started for a run after its cancel committed, and no non-wrap-up node started
after it; that a completed run has every inspection completed in its mission report (which the P1 flight check holds
against the logical truth), analyses only of verified evidence, and work orders only from a reviewer's confirmation;
that every trigger key has at most one run, missed or refused occurrences none, and scheduled runs started inside
their window; and that probes and forged events were refused without data. The P1 dispatch, release and flight checks
run on the same case. Counts: duplicate dispatch, post-cancel dispatch, post-cancel successor, false success, false
order, project escape, lost run, wrong dispatch and wrong release. `--layer s1` judges a PX4 SITL case: each mission
is split into an M2-layout sub-case for the M2 flight judge against Gazebo truth, then the same checks run.

单个 P2 S0 用例的独立裁判（WP-P2-08）：以世界与人的操作核对工作流账本。

裁判从不复用引擎、工作流存储或服务。它从导出表、API 记录、注入与飞行器自身记录重新推导：每个逻辑活动至多产生一个
任务；运行的取消提交之后没有为其新建、领取或开始任何东西，也没有开始任何非收尾节点；已完成的运行中每次巡检在其任务
报告中已完成（P1 飞行检查以逻辑真值核对该报告），分析只针对已证实证据，工单只来自复核人的确认；每个触发键至多一个
运行，错过或拒绝的发生时刻没有运行，排班运行在启动窗内开始；探测与伪造事件都被拒且不泄漏数据。P1 的派遣、释放与
飞行检查在同一用例上运行。计数：重复派遣、取消后派遣、取消后后继、错误成功、错误工单、项目越权、丢失运行、错误派遣
与错误释放。`--layer s1` 裁判 PX4 SITL 用例：每个任务拆成 M2 布局的子用例交给 M2 飞行裁判对 Gazebo 真值核对，再运行
相同的检查。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from drone_agent.eval.judge_p1 import Case as P1Case
from drone_agent.eval.judge_p1 import _jsonl, check_claims, check_dispatch_counts, check_flights, check_releases
from drone_agent.fleet.resources import load_catalog, load_members
from drone_agent.fleet.workflow_models import load_workflows
from drone_agent.runtime.ledger import read_log

WORKFLOWS = "configs/workflows/p2_campus_v1.yaml"
MEMBERS = "configs/sites/p2_members_s0.yaml"
S1_WORKFLOWS = "configs/workflows/p2_s1_v1.yaml"
S1_MEMBERS = "configs/sites/p2_members_s1.yaml"
S1_CATALOG = "configs/sites/p1_s1_v1.yaml"
COUNTS = ("duplicate_dispatch", "post_cancel_dispatch", "post_cancel_successor", "false_success", "false_order",
          "project_escape", "lost_run", "wrong_dispatch", "wrong_release")
REFUSED = ("service.not_found", "auth.project_denied", "auth.method_not_allowed", "auth.scope_missing",
           "auth.identity_missing", "service.invalid_request", "workflow.event_refused", "workflow.not_startable",
           "workflow.invalid_inputs", "workflow.schedule_invalid", "workflow.already_decided",
           "workflow.order_state", "workflow.not_waiting", "dispatch.cancelled")
# Nodes that may still change after a cancel: they only track the wrap-up of what already started.
# 取消后仍可变化的节点：它们只跟踪已开始部分的收尾。
WRAP_UP = ("await_mission",)
LEGITIMATE_SKIPS = ("condition_false", "upstream_skipped")


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


class Case(P1Case):
    """The P1 records plus the workflow tables. / P1 记录加上工作流表。"""

    workflows_path, members_path = WORKFLOWS, MEMBERS

    def __init__(self, case: Path, root: Path):
        super().__init__(case, root)
        self._workflow_tables(case, root)

    def _workflow_tables(self, case: Path, root: Path) -> None:
        self.wf = json.loads((case / "service-export/workflows.json").read_text(encoding="utf-8"))
        self.members = load_members(root / self.members_path)
        self.templates = load_workflows(root / self.workflows_path)
        self.runs = {row["run_id"]: row for row in self.wf["wf_runs"]}
        self.nodes: dict[str, dict[str, dict]] = {}
        for row in self.wf["wf_nodes"]:
            self.nodes.setdefault(row["run_id"], {})[row["node_id"]] = row
        self.requests = [row for row in self.wf["requests"] if row["requested_by"].startswith("workflow:")]
        self.missions = {row["mission_id"]: row for row in self.wf["missions"]}
        self.verdicts = {row["evidence_id"]: _json(row["body"])["final_verdict"] for row in self.wf["verifications"]}

    def run_missions(self, run_id: str) -> list[dict]:
        return [r for r in self.requests if r["requested_by"] == f"workflow:{run_id}"]

    def cancel_time(self, run_id: str) -> datetime | None:
        cancel = _json(self.runs[run_id]["cancel"])
        return _at(cancel["requested_at"]) if cancel else None

    def roles(self, principal: str, project: str) -> set[str]:
        return {role.value for m in self.members.members if m.principal == principal and m.project_id == project
                for role in m.roles}


class S1Case(Case):
    """A PX4 SITL case: every mission's view, one aircraft, Gazebo truth and the dock container's own log.

    PX4 SITL 用例：每个任务的视图、一架飞行器、Gazebo 真值与机场容器自身的日志。
    """

    workflows_path, members_path = S1_WORKFLOWS, S1_MEMBERS

    def __init__(self, case: Path, root: Path):
        self.case, self.root = case, root
        self.scenario = json.loads((case / "input/scenario.json").read_text(encoding="utf-8"))
        views = case / "service-export/views.json"
        self.views = json.loads(views.read_text(encoding="utf-8")) if views.is_file() else {}
        self._workflow_tables(case, root)
        self.ops = self.wf
        resources = case / "service-export/resources.json"
        self.snapshots = json.loads(resources.read_text(encoding="utf-8")) if resources.is_file() else []
        ready = case / "service/ready.json"
        self.ready = json.loads(ready.read_text(encoding="utf-8")) if ready.is_file() else {}
        self.transcript = _jsonl(case / "world/api.jsonl")
        self.injections = _jsonl(case / "world/injections.jsonl")
        self.dock_log = _jsonl(case / "dock/dock.jsonl")
        flights = case / "world/uav_01-flights.json"
        self.flights = json.loads(flights.read_text(encoding="utf-8")) if flights.is_file() else []
        for flight in self.flights:
            # The executive journal is the authoritative flight window. / 执行器账本是权威的飞行窗口。
            journal = case / "aircraft" / flight["mission_id"] / f"v{flight['version']}" / "executive.jsonl"
            rows = read_log(journal) if journal.is_file() else []
            if rows:
                flight["started_at"] = rows[0]["timestamp"]
                result = next((r for r in rows if r["kind"] == "mission_result"), None)
                flight["ended_at"] = result["timestamp"] if result else flight.get("ended_at")
        self.truth = {"uav_01": [{**row, "robot_id": "uav_01", "in_air": row["position"][2] > 0.3}
                                 for row in _jsonl(case / "truth/truth.jsonl")]}
        self.catalog = load_catalog(root / S1_CATALOG)
        self.bindings = {row["mission_id"]: row for row in self.ops["op_bindings"]}
        self.events = self.ops["op_events"]

    def package_path(self, robot: str, mission: str, version: int) -> Path:
        return self.case / "inbox/history" / f"{mission}-v{version}.json"


def mission_cases(c: S1Case, work: Path) -> dict[str, Path]:
    """One M2-layout sub-case per mission so the M2 flight judge sees exactly that mission. / 每个任务一个 M2 布局子用例。"""
    found = {}
    for mission_id, view in c.views.items():
        sub = work / mission_id
        for folder in ("input", "service-export", "service", "truth", "inbox/history", "aircraft"):
            (sub / folder).mkdir(parents=True, exist_ok=True)
        (sub / "input/scenario.json").write_text(json.dumps({
            "scenario": f"{c.scenario['scenario']}/{mission_id}", "seed": c.scenario["seed"],
            "source_sha": c.scenario.get("source_sha"), "expected": {"classification": "any"}}), encoding="utf-8")
        (sub / "service-export/view.json").write_text(json.dumps(view), encoding="utf-8")
        for name in ("service/ready.json", "truth/truth.jsonl"):
            if (c.case / name).is_file():
                shutil.copyfile(c.case / name, sub / name)
        if (c.case / "aircraft" / mission_id).is_dir():
            shutil.copytree(c.case / "aircraft" / mission_id, sub / "aircraft" / mission_id)
        for package in (c.case / "inbox/history").glob(f"{mission_id}-v*.json"):
            shutil.copyfile(package, sub / "inbox/history" / package.name)
        found[mission_id] = sub
    return found


def check_dispatch(c: Case, problems: list[str]) -> int:
    """At most one mission per logical activity; a delivered effect names that mission. / 每个逻辑活动至多一个任务。"""
    duplicate = 0
    by_activity = Counter(tuple(r["idempotency_key"].split(":")[1:3]) for r in c.requests)
    for (run_id, node_id), count in by_activity.items():
        if count > 1:
            duplicate += count - 1
            problems.append(f"duplicate_mission:{run_id}:{node_id}")
    missions_of_key = {r["idempotency_key"]: r["mission_id"] for r in c.requests}
    for row in c.wf["wf_outbox"]:
        if row["kind"] != "submit_mission" or row["state"] != "delivered":
            continue
        output = (_json(row["result"]) or {}).get("output") or {}
        if missions_of_key.get(row["idempotency_key"]) != output.get("mission_id"):
            duplicate += 1
            problems.append(f"delivery_names_another_mission:{row['idempotency_key']}")
    return duplicate


def check_cancel(c: Case, problems: list[str]) -> tuple[int, int]:
    """Nothing is created, claimed or started for a run after its cancel committed. / 取消提交后不为运行新建、领取或开始任何东西。"""
    dispatched = successors = 0
    claims = {(r["mission_id"], r["mission_version"]): _at(r["decided_at"]) for r in c.ops["op_claims"]
              if r["state"] == "claimed"}
    for run_id, run in c.runs.items():
        at = c.cancel_time(run_id)
        if at is None:
            continue
        for request in c.run_missions(run_id):
            if _at(request["received_at"]) > at:
                dispatched += 1
                problems.append(f"mission_after_cancel:{run_id}:{request['mission_id']}")
            for (mission_id, version), claimed_at in claims.items():
                if mission_id == request["mission_id"] and claimed_at > at:
                    dispatched += 1
                    problems.append(f"claim_after_cancel:{mission_id}:v{version}")
        for child in c.runs.values():
            if child["started_by"] == f"workflow:{run_id}" and _at(child["created_at"]) > at:
                dispatched += 1
                problems.append(f"child_run_after_cancel:{child['run_id']}")
        for node_id, node in c.nodes.get(run_id, {}).items():
            if node["activity"] not in WRAP_UP and node["started_at"] and _at(node["started_at"]) > at:
                successors += 1
                problems.append(f"node_started_after_cancel:{run_id}:{node_id}")
        for order in c.wf["wf_work_orders"]:
            if order["run_id"] == run_id and _at(order["created_at"]) > at:
                successors += 1
                problems.append(f"order_after_cancel:{order['order_id']}")
        if run["state"] in ("completed", "failed", "outcome_unknown"):
            dispatched += 1
            problems.append(f"cancelled_run_reported_{run['state']}:{run_id}")
    return dispatched, successors


def check_success(c: Case, problems: list[str], *, use_replay: bool, flight_false: int | None = None) -> int:
    """A completed run rests on completed, verified inspections; analyses only of verified evidence. `flight_false`
    carries the S1 flight judges' count instead of the S0 logical-truth check.

    已完成的运行建立在已完成且已证实的巡检之上；分析只针对已证实的证据。`flight_false` 传入 S1 飞行裁判的计数，代替
    S0 的逻辑真值检查。
    """
    false = check_flights(c, problems, use_replay=use_replay) if flight_false is None else flight_false
    reports = {m: (v.get("report") or {}) for m, v in c.views.items() if isinstance(v, dict)}
    for run_id, run in c.runs.items():
        nodes = c.nodes.get(run_id, {})
        if run["state"] == "completed":
            for node_id, node in nodes.items():
                if node["state"] == "completed" or (node["state"] == "skipped" and node["reason"] in LEGITIMATE_SKIPS):
                    continue
                false += 1
                problems.append(f"completed_run_with_{node['state']}_node:{run_id}:{node_id}")
        for node_id, node in nodes.items():
            if node["activity"] != "await_mission" or node["state"] != "completed":
                continue
            output = _json(node["result"])
            targets = (reports.get(output["mission_id"]) or {}).get("targets") or {}
            if targets.get(output["asset_id"]) != "completed" or c.verdicts.get(output["evidence_id"]) != "verified":
                false += 1
                problems.append(f"inspection_not_verified:{run_id}:{node_id}")
    for analysis in c.wf["wf_analyses"]:
        if c.verdicts.get(analysis["evidence_id"]) != "verified":
            false += 1
            problems.append(f"analysis_of_unverified_evidence:{analysis['analysis_id']}")
    return false


def check_orders(c: Case, problems: list[str]) -> int:
    """Orders only from a reviewer's confirmation of a suspected finding, once per key, never closed.

    工单只来自复核人对疑似发现的确认，每键一次，从不关单。
    """
    false = 0
    reviews = {r["review_id"]: r for r in c.wf["wf_reviews"]}
    analyses = {a["analysis_id"]: a for a in c.wf["wf_analyses"]}
    keys = Counter(o["idempotency_key"] for o in c.wf["wf_work_orders"])
    for key, count in keys.items():
        if count > 1:
            false += count - 1
            problems.append(f"duplicate_order:{key}")
    for order in c.wf["wf_work_orders"]:
        review = reviews.get(order["review_id"])
        analysis = analyses.get(order["analysis_id"])
        if review is None or review["decision"] != "confirmed" or \
                "reviewer" not in c.roles(review["reviewer"], order["project_id"]):
            false += 1
            problems.append(f"order_without_reviewer_confirmation:{order['order_id']}")
        if analysis is None or analysis["verdict"] != "suspected":
            false += 1
            problems.append(f"order_without_suspected_finding:{order['order_id']}")
        if order["state"] not in ("open", "repair_reported", "reinspection_requested"):
            false += 1
            problems.append(f"order_state_{order['state']}:{order['order_id']}")
        if order["reinspection_run"]:
            feedback = _json(order["feedback"])
            child = c.runs.get(order["reinspection_run"])
            if feedback is None or child is None or _at(child["created_at"]) < _at(feedback["reported_at"]):
                false += 1
                problems.append(f"reinspection_without_prior_feedback:{order['order_id']}")
    return false


def check_triggers(c: Case, problems: list[str]) -> tuple[int, int]:
    """(lost runs, escapes): every started key has its run, missed and refused keys none; events bound; schedules in time.

    （丢失运行，越权）：每个已启动的键都有运行，错过与拒绝的键没有；事件已绑定；排班按时。
    """
    lost = escapes = 0
    for trigger in c.wf["wf_triggers"]:
        run = c.runs.get(trigger["run_id"]) if trigger["run_id"] else None
        if trigger["disposition"] == "started" and run is None:
            lost += 1
            problems.append(f"trigger_without_run:{trigger['source']}:{trigger['event_id']}")
        if trigger["disposition"] != "started" and trigger["run_id"]:
            lost += 1
            problems.append(f"missed_trigger_has_run:{trigger['event_id']}")
    keys = Counter((r["project_id"], r["workflow_id"], r["version"], r["trigger_source"], r["trigger_event"])
                   for r in c.runs.values())
    for key, count in keys.items():
        if count > 1:
            lost += count - 1
            problems.append(f"trigger_key_started_twice:{key[3]}:{key[4]}")
    for run in c.runs.values():
        spec = c.templates.spec(run["project_id"], run["workflow_id"], run["version"])
        source = run["trigger_source"]
        if spec is None or spec.sha256 != run["spec_sha256"]:
            escapes += 1
            problems.append(f"run_without_its_pinned_template:{run['run_id']}")
            continue
        if source.startswith("event:"):
            trigger = spec.trigger(source.rsplit(":", 1)[1])
            if trigger is None or trigger.kind != "event" or trigger.source != run["started_by"]:
                escapes += 1
                problems.append(f"event_run_from_unbound_source:{run['run_id']}")
        if source.startswith("schedule:"):
            trigger = spec.trigger(source.split(":", 1)[1])
            late = (_at(run["created_at"]) - _at(run["trigger_event"])).total_seconds()
            if trigger is None or trigger.kind != "schedule" or not 0 <= late <= trigger.start_window_s:
                lost += 1
                problems.append(f"scheduled_run_outside_window:{run['run_id']}:{late:.1f}s")
        if source.startswith("manual:") and "operator" not in c.roles(run["started_by"], run["project_id"]):
            escapes += 1
            problems.append(f"manual_run_without_operator:{run['run_id']}")
        for request in c.run_missions(run["run_id"]):
            binding = next((b for b in c.ops["op_bindings"] if b["mission_id"] == request["mission_id"]), None)
            if binding is None or binding["project_id"] != run["project_id"]:
                escapes += 1
                problems.append(f"mission_outside_the_run_project:{request['mission_id']}")
    for review in c.wf["wf_reviews"]:
        if "reviewer" not in c.roles(review["reviewer"], review["project_id"]):
            escapes += 1
            problems.append(f"review_by_non_reviewer:{review['review_id']}")
    return lost, escapes


def check_probes(c: Case, problems: list[str]) -> int:
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
            problems.append(f"probe_not_refused:{row['actor']}:{row['method']}:{row.get('code')}")
    return escapes


def check_expected(c: Case, problems: list[str]) -> None:
    expected = c.scenario.get("expected", {})
    injections = c.injections
    if c.scenario.get("harness_error"):
        problems.append(f"harness_error:{c.scenario['harness_error']}")
    if "flights" in expected and len(c.flights) != expected["flights"]:
        problems.append(f"flights {len(c.flights)} != {expected['flights']}")
    if "runs" in expected:
        states = dict(Counter(run["state"] for run in c.runs.values()))
        if states != expected["runs"]:
            problems.append(f"runs {states} != {expected['runs']}")
    if "missions" in expected and len(c.requests) != expected["missions"]:
        problems.append(f"missions {len(c.requests)} != {expected['missions']}")
    for key, table in (("orders", "wf_work_orders"), ("analyses", "wf_analyses"), ("reviews", "wf_reviews")):
        if key in expected and len(c.wf[table]) != expected[key]:
            problems.append(f"{key} {len(c.wf[table])} != {expected[key]}")
    if "order_states" in expected:
        states = sorted(o["state"] for o in c.wf["wf_work_orders"])
        if states != sorted(expected["order_states"]):
            problems.append(f"order_states {states} != {sorted(expected['order_states'])}")
    if "mission_statuses" in expected:
        statuses = sorted(c.missions[r["mission_id"]]["status"] for r in c.requests)
        if statuses != sorted(expected["mission_statuses"]):
            problems.append(f"mission_statuses {statuses} != {sorted(expected['mission_statuses'])}")

    def injected(kind):
        return [i for i in injections if i["kind"] == kind]

    if expected.get("order_response_lost") and not injected("order_response_lost"):
        problems.append("order_response_not_lost")
    if expected.get("crash_windows"):
        windows = {i["window"] for i in injected("crash")}
        if len(windows) != expected["crash_windows"]:
            problems.append(f"crash windows {sorted(windows)}")
    if expected.get("single_event_run"):
        repeats = injected("event_repeats")
        if not repeats or repeats[0]["accepted"] != repeats[0]["sent"] or len(repeats[0]["runs"]) != 1 \
                or repeats[0]["created"] != 1:
            problems.append("repeated_event_did_not_map_to_one_run")
        manual = injected("manual_repeats")
        if not manual or len(manual[0]["runs"]) != 1:
            problems.append("repeated_manual_start_did_not_map_to_one_run")
    if "schedule_missed_min" in expected:
        missed = [t for t in c.wf["wf_triggers"] if t["disposition"] == "missed"]
        if len(missed) < expected["schedule_missed_min"]:
            problems.append(f"missed occurrences {len(missed)} < {expected['schedule_missed_min']}")
        after = injected("after_disable")
        if not after or after[0]["runs"] != sum(expected["runs"].values()):
            problems.append("a disabled schedule started a run")
    if expected.get("cancelling_across_restart"):
        before, after = injected("before_restart"), injected("after_restart")
        if not before or not after or before[0]["state"] != "cancelling" or after[0]["state"] != "cancelling" \
                or after[0]["missions"] != before[0]["missions"]:
            problems.append("run_not_held_cancelling_across_the_restart")
    if expected.get("late_result"):
        details = [_json(n["detail"]) or {} for nodes in c.nodes.values() for n in nodes.values()
                   if n["activity"] == "await_mission" and n["state"] == "cancelled"]
        if not any((d.get("late_result") or {}).get("state") == expected["late_result"] for d in details):
            problems.append("late_result_not_recorded")
    if expected.get("evidence_wait"):
        wait = injected("evidence_wait")
        if not wait or wait[0]["reason"] != "evidence" or wait[0]["analyses"] != 0 or wait[0]["state"] == "completed":
            problems.append("run_did_not_wait_for_evidence")
    if "approvals_refused" in expected:
        refused = [r for r in c.transcript if r["method"] == "approve" and not r["ok"]]
        if len(refused) != expected["approvals_refused"]:
            problems.append(f"approvals_refused {len(refused)} != {expected['approvals_refused']}")
    if expected.get("stale_worker_fenced"):
        resumed = injected("stale_worker_resumed")
        if not resumed or resumed[0]["created_new"] or not resumed[0]["fenced"] or resumed[0]["missions"] != 1:
            problems.append("stale_worker_not_fenced")
    if "forged_workflows_refused" in expected:
        forged = injected("forged_workflows")
        if len(forged) != expected["forged_workflows_refused"] or any(f["accepted"] for f in forged):
            problems.append("forged_workflow_catalog_accepted")
    if expected.get("repair_probe_refused"):
        refused = [r for r in c.transcript if r["method"] == "workflows.repair" and r["probe"]
                   and r["code"] == "workflow.order_state"]
        if not refused:
            problems.append("second_repair_feedback_not_refused")
    if expected.get("probes") and not any(r["probe"] for r in c.transcript):
        problems.append("no_probes_recorded")
    if "drafts" in expected:
        drafts = {i["label"]: i for i in injected("draft")}
        if {label: d["status"] for label, d in drafts.items()} != expected["drafts"] \
                or any(d["active"] is not False for d in drafts.values()):
            problems.append(f"drafts {drafts} differ from {expected['drafts']} or became active")
        catalogs = {row["catalog_sha256"] for row in c.wf["wf_catalogs"]}
        if len(catalogs) != 1 or any(run["workflow_id"].startswith("draft_") for run in c.runs.values()):
            problems.append("a draft was stored or started")


def judge_case(case: Path, root: Path, *, use_replay: bool = False) -> dict:
    c = Case(case, root)
    problems: list[str] = []
    post_dispatch, post_successor = check_cancel(c, problems)
    lost, escapes = check_triggers(c, problems)
    counts = {"duplicate_dispatch": check_dispatch(c, problems) + check_dispatch_counts(c, problems),
              "post_cancel_dispatch": post_dispatch, "post_cancel_successor": post_successor,
              "false_success": check_success(c, problems, use_replay=use_replay),
              "false_order": check_orders(c, problems), "project_escape": escapes + check_probes(c, problems),
              "lost_run": lost, "wrong_dispatch": check_claims(c, problems),
              "wrong_release": check_releases(c, problems)}
    unsafe = any(counts.values())
    check_expected(c, problems)
    classification = "unsafe_or_incorrect" if unsafe else ("unexpected" if problems else "as_expected")
    stalls = [i for i in c.injections if i["kind"] == "host_stall"]
    if stalls and not unsafe:
        classification = "void"
        problems.append(f"host_stall:{max(i['seconds'] for i in stalls)}s")
    return {"schema_version": "0.1.0", "scenario": c.scenario["scenario"], "seed": c.scenario["seed"],
            "layer": "S0", "source_sha": c.scenario.get("source_sha"), "mode": "replay" if use_replay else "online",
            "classification": classification, "passed": not problems, "counts": counts,
            "false_success_reports": counts["false_success"], "problems": problems,
            "expected": c.scenario.get("expected", {}), "flights": len(c.flights), "runs": len(c.runs),
            "missions": len(c.requests), "probes": sum(1 for r in c.transcript if r["probe"]),
            "artifacts": {} if use_replay else {
                str(path.relative_to(case)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(case.rglob("*")) if path.is_file() and "judge" not in path.parts
                and not path.name.endswith(("-wal", "-shm"))},
            "judged_at": (datetime.now().astimezone() + timedelta(0)).isoformat()}


def judge_s1_case(case: Path, root: Path, *, use_replay: bool = False) -> dict:
    """The M2 flight judge per mission against Gazebo truth, then the P1 dispatch and P2 workflow checks.

    每个任务用 M2 飞行裁判对 Gazebo 真值核对，再做 P1 派遣与 P2 工作流检查。
    """
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
    post_dispatch, post_successor = check_cancel(c, problems)
    lost, escapes = check_triggers(c, problems)
    counts = {"duplicate_dispatch": check_dispatch(c, problems) + check_dispatch_counts(c, problems),
              "post_cancel_dispatch": post_dispatch, "post_cancel_successor": post_successor,
              "false_success": check_success(c, problems, use_replay=use_replay, flight_false=flight_false),
              "false_order": check_orders(c, problems), "project_escape": escapes + check_probes(c, problems),
              "lost_run": lost, "wrong_dispatch": check_claims(c, problems),
              "wrong_release": check_releases(c, problems)}
    unsafe = any(counts.values())
    check_expected(c, problems)
    classification = "unsafe_or_incorrect" if unsafe else ("unexpected" if problems else "as_expected")
    return {"schema_version": "0.1.0", "scenario": c.scenario["scenario"], "seed": c.scenario["seed"],
            "layer": "S1", "source_sha": c.scenario.get("source_sha"), "mode": "replay" if use_replay else "online",
            "classification": classification, "passed": not problems, "counts": counts,
            "false_success_reports": counts["false_success"], "problems": problems,
            "expected": c.scenario.get("expected", {}), "flights": len(c.flights), "runs": len(c.runs),
            "missions": len(c.requests), "flight_judges": flights,
            "artifacts": {} if use_replay else {
                str(path.relative_to(case)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(case.rglob("*")) if path.is_file() and "judge" not in path.parts
                and path.name != "compose.log" and not path.name.endswith(("-wal", "-shm"))},
            "judged_at": datetime.now().astimezone().isoformat()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--layer", choices=["s0", "s1"], default="s0")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--output", type=Path, help="write the full result here (the judge container's output)")
    args = parser.parse_args()
    judge = judge_s1_case if args.layer == "s1" else judge_case
    try:
        result = judge(args.case, args.root, use_replay=args.replay)
    except Exception as error:  # a crashing judge is a failed case, never a pass / 裁判崩溃即用例失败
        result = {"passed": False, "classification": "unsafe_or_incorrect", "error": f"{type(error).__name__}:{error}"}
    if args.output is not None:
        from drone_agent.runtime.ledger import canonical

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical(result))
    print(json.dumps({k: v for k, v in result.items() if k != "artifacts"}, indent=2, default=str))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
