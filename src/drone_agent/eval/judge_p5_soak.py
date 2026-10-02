"""Independent judge of the P5 72-hour soak (D073), run offline after it ended.

It reads only records: the soak's manifest, expanded occurrences, final state, action journal and resource samples; a
copy of the ledger taken at judging time; the simulators' own truth (the desk flights and their world placements, the
logical fleet's flights and captures, the vendor simulator's executions and captures) and the world history the harness
wrote. It recomputes the planned occurrences of every enabled service schedule from the schedule itself and the time it
was enabled, and decides the seven frozen criteria of `configs/soak/p5_soak_v1.yaml`:

1. duration: one deployment for at least `min_duration_h`, the service always on the manifest's revision, the ledger
   never reset (growing tables never shrink, records from before T0 still present);
2. planned vs actual: every planned occurrence has exactly one outcome; a schedule occurrence is missed only while the
   service was down for its whole start window;
3. no lost run: every run is final, or active with a stated wait;
4. zero counts: unknown reported as success, dispatch after cancel, duplicate dispatch, permission escape, false
   closure, duplicate finding or order, vendor duplicate execution, acknowledgement taken as completion;
5. recovery after each fault: service ready, dock sessions active and cut-off robots reporting within the limits; model
   fault windows end planning and analyses with stated reasons, and calls succeed outside them;
6. resources: no restart beyond the injected ones, no out-of-memory kill, every burst drained in `backlog_drain_h`;
7. layer counts: S0 / S1 / S2 / S3 separately, with the S1 flight, S3 task and S2 probe minimums.

A criterion without evidence is `missing`, never `passed`. The harness's actions count as harness intervention; none of
this proves a real device, a real vendor's firmware, field networks or any model accuracy.

P5 72 小时长稳的独立裁判（D073），在长稳结束后离线运行。它只读取记录：长稳的 manifest、展开的发生时刻、最终状态、动作日志
与资源采样；裁判时复制的账本副本；模拟器自身的真值（任务台飞行及其世界放置、逻辑机队的飞行与拍摄、厂商模拟器的执行与拍摄）
以及编排写下的世界历史。它根据排班本身与其启用时刻重算每个已启用服务排班的计划发生时刻，并判定 `configs/soak/p5_soak_v1.yaml`
冻结的七条判据（见上文英文）。没有证据的判据为 `missing`，绝不是 `passed`。编排的动作计为编排介入；这些都不证明真实设备、真实
厂商固件、现场网络或任何模型精度。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

GROWING = ("missions", "events", "op_events", "op_claims", "wf_runs", "wf_triggers", "bz_jobs", "bz_findings",
           "bz_orders", "reports")
RUN_FINAL = ("completed", "failed", "outcome_unknown", "cancelled")
FINAL_OCCURRENCE = ("done", "closed", "no_order", "not_closed", "timeout", "failed", "missed")
LAYER_OF_PROJECT = {"fleet_s0": "S0", "campus_s1": "S1", "vendor_s3": "S3"}
COUNTS = ("unknown_as_success", "post_cancel_dispatch", "duplicate_dispatch", "permission_escape", "false_closure",
          "duplicate_finding", "duplicate_order", "vendor_duplicate_execution", "ack_as_completion",
          "unexplained_miss", "duplicate_start", "lost_run", "lost_occurrence")


def _at(value) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _json(value):
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return None


def _jsonl(path: Path) -> list[dict]:
    rows = []
    for candidate in (path.with_name(path.name + ".1"), path):
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
    return rows


class Records:
    """Everything the judge reads, loaded once. / 裁判读取的全部记录，一次加载。"""

    def __init__(self, soak: Path, ledger: Path, *, flights: Path | None, supervisor: Path | None, fleet: Path | None,
                 vendor: Path | None, world: Path | None):
        self.manifest = json.loads((soak / "manifest.json").read_text(encoding="utf-8"))
        self.plan = self.manifest["plan"]
        self.occurrences = json.loads((soak / "occurrences.json").read_text(encoding="utf-8"))
        state = soak / "state.json"
        self.state = json.loads(state.read_text(encoding="utf-8")) if state.is_file() else {}
        finished = soak / "finished.json"
        self.finished = json.loads(finished.read_text(encoding="utf-8")) if finished.is_file() else None
        self.actions = _jsonl(soak / "actions.jsonl")
        self.samples = _jsonl(soak / "samples.jsonl")
        self.t0, self.end = _at(self.manifest["t0"]), _at(self.manifest["end"])
        db = sqlite3.connect(f"file:{ledger.as_posix()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.tables = {name: [dict(row) for row in db.execute(f'SELECT * FROM "{name}"')]
                           for name in ("wf_runs", "wf_triggers", "wf_schedules", "missions", "op_claims",
                                        "op_bindings", "evidence", "verifications", "reports", "bz_findings",
                                        "bz_orders", "bz_rounds", "wf_outbox", "requests", "sc_assignments")
                           if name in names}
            self.vendor_events = [dict(row) for row in db.execute(
                "SELECT mission_id, mission_version, seq, kind, body, timestamp FROM events WHERE journal='vendor'")] \
                if "events" in names else []
            self.counts = {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                           for name in GROWING if name in names}
        finally:
            db.close()
        self.flight_records = []
        if flights is not None and flights.is_dir():
            for path in sorted(flights.glob("m-*-v*/flight.json")):
                self.flight_records.append(json.loads(path.read_text(encoding="utf-8")))
        self.mission_records = {}
        if supervisor is not None and (supervisor / "missions").is_dir():
            for path in sorted((supervisor / "missions").glob("m-*.json")):
                self.mission_records[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        self.fleet_flights, self.fleet_captures = [], []
        if fleet is not None and fleet.is_dir():
            for robot in sorted(p for p in fleet.iterdir() if p.is_dir() and p.name.startswith("uav_")):
                self.fleet_flights += [{**row, "robot_id": row.get("robot_id", robot.name)}
                                       for row in _jsonl(robot / "flights.jsonl")]
            self.fleet_captures = _jsonl(fleet / "captures.jsonl")
        self.vendor_truth = _jsonl(vendor / "truth.jsonl") if vendor is not None else []
        self.world_history = _jsonl(world / "history.jsonl") if world is not None else []
        self.bindings = {row["mission_id"]: row for row in self.tables.get("op_bindings", [])}
        self.runs = {row["run_id"]: row for row in self.tables.get("wf_runs", [])}

    def during(self, rows: list[dict], key: str) -> list[dict]:
        return [r for r in rows if (at := _at(r.get(key))) is not None and self.t0 <= at <= (self.end_at())]

    def end_at(self) -> datetime:
        return _at(self.finished["finished_at"]) if self.finished else self.end

    def service_up(self, start: datetime, stop: datetime) -> bool:
        """Whether any sample saw a ready service inside the interval. / 区间内是否有采样看到服务就绪。"""
        return any(start <= _at(s["at"]) <= stop and (s.get("service") or {}).get("ok") for s in self.samples)


# ── the criteria / 判据 ──


def duration(r: Records) -> dict:
    criteria = r.plan["criteria"]
    if r.finished is None:
        return {"status": "missing", "reason": "the soak has not finished"}
    hours = (r.end_at() - r.t0).total_seconds() / 3600
    revisions = sorted({(s.get("service") or {}).get("source_sha") for s in r.samples
                        if (s.get("service") or {}).get("ok")} - {None})
    shrunk = []
    previous: dict[str, int] = {}
    for sample in r.samples:
        for table in GROWING:
            value = (sample.get("ledger") or {}).get(table)
            if value is None:
                continue
            if value < previous.get(table, 0):
                shrunk.append({"table": table, "at": sample["at"], "from": previous[table], "to": value})
            previous[table] = value
    before_t0 = sum(1 for m in r.tables.get("missions", []) if _at(m["created_at"]) < r.t0)
    first = (r.samples[0].get("ledger") or {}) if r.samples else {}
    ok = (r.manifest.get("full_length") and hours >= criteria["min_duration_h"] and revisions == [
        r.manifest["source_sha"]] and not shrunk and before_t0 > 0 and bool(r.samples)
        and all(r.counts.get(t, 0) >= first.get(t, 0) for t in GROWING))
    return {"status": "passed" if ok else "failed", "hours": round(hours, 3), "full_length": r.manifest.get("full_length"),
            "service_revisions": revisions, "shrunk": shrunk[:20], "missions_before_t0": before_t0,
            "samples": len(r.samples)}


def interval_occurrences(minutes: int, start: datetime, stop: datetime) -> list[datetime]:
    """UTC multiples of the interval in (start, stop]: the schedule's own anchoring. / 区间内的 UTC 整倍数时刻。"""
    step = timedelta(minutes=minutes)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    first = epoch + step * ((start - epoch) // step + 1)
    found = []
    while first <= stop:
        found.append(first)
        first += step
    return found


def schedules(r: Records, counts: Counter) -> dict:
    """Every occurrence of every planned schedule between enabling and the end has exactly one trigger row.

    每个计划排班在启用与结束之间的每个发生时刻都恰有一条触发记录。
    """
    enabled = {(e["project_id"], e["workflow_id"], e["trigger_id"]): e for e in (r.state.get("schedules") or [])}
    if not enabled:
        return {"status": "missing", "reason": "no schedule was enabled"}
    triggers = defaultdict(list)
    for row in r.tables.get("wf_triggers", []):
        if str(row["source"]).startswith("schedule:"):
            triggers[(row["project_id"], row["workflow_id"], row["source"].split(":", 1)[1])].append(row)
    report, ok = {}, True
    for key, entry in enabled.items():
        spec = next((s for s in r.plan["schedules"] if (s["project_id"], s["workflow_id"], s["trigger_id"]) == key),
                    None)
        minutes, window = (spec or {}).get("interval_min", 30), (spec or {}).get("start_window_s", 300)
        start, stop = _at(entry["at"]), r.end_at()
        expected = interval_occurrences(minutes, start, stop - timedelta(seconds=window))
        rows = {}
        duplicates = 0
        for row in triggers.get(key, []):
            occurrence = _at(row["event_id"])
            if occurrence is None:
                continue
            if occurrence in rows:
                duplicates += 1
            rows[occurrence] = row
        missing = [o for o in expected if o not in rows]
        missed = [o for o in expected if o in rows and rows[o]["disposition"] == "missed"]
        unexplained = [o for o in missed if r.service_up(o + timedelta(seconds=5), o + timedelta(seconds=window - 5))]
        started = [o for o in expected if o in rows and rows[o]["disposition"] == "started"]
        counts["unexplained_miss"] += len(unexplained) + len(missing)
        counts["duplicate_start"] += duplicates
        ok = ok and not missing and not unexplained and not duplicates and spec is not None and entry.get("ok")
        report["/".join(key)] = {"expected": len(expected), "started": len(started), "missed": len(missed),
                                 "missing": [o.isoformat() for o in missing][:10],
                                 "unexplained_missed": [o.isoformat() for o in unexplained][:10],
                                 "duplicates": duplicates, "layer": (spec or {}).get("layer")}
    return {"status": "passed" if ok else "failed", "schedules": report}


def occurrences(r: Records, counts: Counter) -> dict:
    """Every harness occurrence ended with one outcome. / 每个编排发生时刻都以一个结果结束。"""
    statuses = r.state.get("occurrences") or {}
    tally: dict[str, Counter] = defaultdict(Counter)
    lost = []
    for item in r.occurrences:
        status = (statuses.get(item["id"]) or {}).get("status", "pending")
        tally[item["kind"]][status] += 1
        if status not in FINAL_OCCURRENCE:
            lost.append(item["id"])
    harness_missed = [i for i, s in statuses.items() if s.get("status") == "missed"]
    counts["lost_occurrence"] += len(lost)
    return {"status": "passed" if not lost and r.finished else "failed",
            "by_kind": {k: dict(v) for k, v in sorted(tally.items())}, "lost": lost[:20],
            "harness_missed": harness_missed[:20]}


def runs(r: Records, counts: Counter) -> dict:
    """No run started during the soak is lost; every one is final or waiting with a reason.

    长稳期间启动的运行没有丢失；每个都已终结或带原因等待。
    """
    tracked = (r.state.get("runs") or {}).keys()
    lost = [run_id for run_id in tracked if run_id not in r.runs]
    soak_runs = r.during(list(r.runs.values()), "created_at")
    open_runs = [run for run in soak_runs if run["state"] not in RUN_FINAL]
    counts["lost_run"] += len(lost)
    states = Counter(run["state"] for run in soak_runs)
    return {"status": "passed" if not lost else "failed", "runs": len(soak_runs), "states": dict(states),
            "lost": lost[:20], "open_at_end": [{k: run[k] for k in ("run_id", "workflow_id", "state", "updated_at")}
                                                for run in open_runs][:20]}


def safety(r: Records, counts: Counter) -> dict:
    """The zero counts. / 必须为 0 的计数。"""
    problems = []
    # Unknown never reported as success: a completed row needs succeeded and verified.
    # 未知从不报告为成功：已完成的行需要 succeeded 且 verified。
    for row in r.during(r.tables.get("reports", []), "created_at"):
        body = _json(row["body"]) or {}
        for item in body.get("rows", []):
            if item.get("column") == "completed" and (item.get("execution_status") != "succeeded" or
                                                     item.get("effect_verdict") != "verified" or
                                                     item.get("service_verdict") not in (None, "verified")):
                counts["unknown_as_success"] += 1
                problems.append(f"completed_without_proof:{row['mission_id']}:{item.get('step_id')}")
    # Nothing is claimed for a run's missions after its cancel committed. / 运行取消提交后其任务不再被领取。
    missions_of_run = runs_missions(r)
    claims = defaultdict(list)
    for claim in r.tables.get("op_claims", []):
        if claim["state"] == "claimed":
            claims[(claim["mission_id"], claim["mission_version"])].append(_at(claim["decided_at"]))
    for run in r.runs.values():
        cancel = _json(run.get("cancel")) or {}
        at = _at(cancel.get("requested_at"))
        if at is None:
            continue
        for mission in missions_of_run.get(run["run_id"], []):
            late = [c for (m, _), times in claims.items() if m == mission for c in times if c > at + timedelta(seconds=1)]
            if late:
                counts["post_cancel_dispatch"] += len(late)
                problems.append(f"claim_after_cancel:{run['run_id']}:{mission}")
    # One mission per workflow activity, one claimed mission per scheduler task, one claim per version.
    # 每个工作流活动一个任务，每个调度任务单一个被领取的任务，每个版本一次领取。
    # A withdrawn and reassigned task makes a second mission legitimately; only a second *claimed* one counts.
    # 撤回并重新分配的任务单会正当地产生第二个任务；只有第二个*被领取*的任务才计数。
    claimed = {m for (m, _), times in claims.items() if times}
    activities: dict[tuple, set] = defaultdict(set)
    for row in r.tables.get("requests", []):
        if str(row["requested_by"]).startswith("workflow:") and row["mission_id"] in claimed:
            activities[tuple(row["idempotency_key"].split("#", 1)[0].split(":")[1:3])].add(row["mission_id"])
    counts["duplicate_dispatch"] += sum(len(m) - 1 for m in activities.values() if len(m) > 1)
    counts["duplicate_dispatch"] += sum(len(times) - 1 for times in claims.values() if len(times) > 1)
    flown = Counter((f.get("mission_id"), f.get("version")) for f in r.fleet_flights if f.get("mission_id"))
    flown.update((rec.get("mission_id"), rec.get("version")) for rec in r.flight_records
                 if rec.get("status") not in ("skipped",))
    counts["duplicate_dispatch"] += sum(n - 1 for n in flown.values() if n > 1)
    # Findings and orders. / 发现与工单。
    open_clusters: dict[str, list[tuple]] = defaultdict(list)
    for finding in r.tables.get("bz_findings", []):
        open_clusters[finding["cluster_key"]].append((_at(finding["created_at"]), _at(finding["closed_at"])))
    for spans in open_clusters.values():
        spans.sort(key=lambda s: s[0])
        for (start, close), (later, _) in zip(spans, spans[1:], strict=False):
            if close is None or later < close:
                counts["duplicate_finding"] += 1
    orders_per_finding = Counter(o["finding_id"] for o in r.tables.get("bz_orders", []))
    counts["duplicate_order"] += sum(n - 1 for n in orders_per_finding.values() if n > 1)
    # Vendor: one execution per flight; completion only with a terminal ok. / 厂商：每个飞行一次执行；只有终态 ok 才算完成。
    executions = Counter(row.get("flight_id") for row in r.vendor_truth if row.get("kind") == "executions")
    counts["vendor_duplicate_execution"] += sum(n - 1 for n in executions.values() if n > 1)
    # The event body carries the whole journal row; its own data holds the vendor's terminal status.
    # 事件正文携带整行账本；行自身的 data 含厂商终态。
    terminal_ok = {(e["mission_id"], e["mission_version"]) for e in r.vendor_events
                   if e["kind"] == "terminal" and ((_json(e["body"]) or {}).get("data") or {}).get("data", {}).get(
                       "status") == "ok"}
    for mission in r.tables.get("missions", []):
        binding = r.bindings.get(mission["mission_id"]) or {}
        if binding.get("execution_backend") == "vendor_protocol_sim" and mission["status"] == "completed" and \
                (mission["mission_id"], mission["current_version"]) not in terminal_ok:
            counts["ack_as_completion"] += 1
            problems.append(f"vendor_completed_without_terminal:{mission['mission_id']}")
    false_closures(r, counts, problems)
    escapes = sum((s.get("escapes") or 0) for s in (r.state.get("occurrences") or {}).values())
    counts["permission_escape"] += escapes
    zero = [k for k in ("unknown_as_success", "post_cancel_dispatch", "duplicate_dispatch", "permission_escape",
                        "false_closure", "duplicate_finding", "duplicate_order", "vendor_duplicate_execution",
                        "ack_as_completion") if counts[k]]
    return {"status": "passed" if not zero else "failed", "nonzero": zero, "problems": problems[:40],
            "authz_probes": sum(1 for i in r.occurrences if i["kind"] == "authz")}


def runs_missions(r: Records) -> dict[str, list[str]]:
    """The missions of each run: its own submissions and the missions of its scheduler tasks.

    每个运行的任务：它自己提交的任务，以及其调度任务单的任务。
    """
    found: dict[str, list[str]] = defaultdict(list)
    for row in r.tables.get("requests", []):
        if str(row["requested_by"]).startswith("workflow:"):
            found[row["requested_by"].split(":", 1)[1]].append(row["mission_id"])
    for row in r.tables.get("wf_outbox", []):
        output = (_json(row.get("result")) or {}).get("output") or {}
        task = output.get("task_id")
        if task:
            found[row["run_id"]] += [a["mission_id"] for a in r.tables.get("sc_assignments", [])
                                     if a["task_id"] == task and a.get("mission_id")]
    return found


def false_closures(r: Records, counts: Counter, problems: list[str]) -> None:
    """A closed order's passing capture was taken while its world showed no damage, by the simulators' own records.

    按模拟器自身记录，已关单工单的通过采集发生在其世界没有损伤之时。
    """
    missions_of_run = runs_missions(r)
    evidence = {row["evidence_id"]: row for row in r.tables.get("evidence", [])}
    by_mission = defaultdict(list)
    for row in evidence.values():
        by_mission[row["mission_id"]].append(row)
    flights = {(f.get("mission_id"), f.get("version")): f for f in r.flight_records}
    for order in r.tables.get("bz_orders", []):
        if order["state"] != "closed":
            continue
        passed = [row for row in r.tables.get("bz_rounds", []) if row["order_id"] == order["order_id"]
                  and row["state"] == "passed"]
        if not passed:
            continue
        run = passed[-1]["reinspection_run"]
        asset = order["asset_key"].rsplit("/", 1)[-1]
        seen = None
        for mission in missions_of_run.get(run, []):
            binding = r.bindings.get(mission) or {}
            layer = LAYER_OF_PROJECT.get(binding.get("project_id"))
            for row in by_mission.get(mission, []):
                at = _at(((_json(row["body"]) or {}).get("time_window") or {}).get("timestamp")) or _at(
                    row["received_at"])
                if layer == "S1":
                    flight = flights.get((mission, row["mission_version"])) or {}
                    placed = [p for p in ((flight.get("world") or {}).get("placed") or []) if p.get("asset_id") == asset
                              and p.get("ok")]
                    seen = "damaged" if placed else "normal" if flight else None
                elif layer == "S0":
                    captures = [c for c in r.fleet_captures if c.get("asset_id") == asset and
                                c.get("robot_id") == binding.get("robot_id") and _at(c["at"]) <= at + timedelta(seconds=5)]
                    seen = captures[-1]["state"] if captures else None
                elif layer == "S3":
                    captures = [c for c in r.vendor_truth if c.get("kind") == "captures" and c.get("asset_id") == asset
                                and _at(c["at"]) <= at + timedelta(seconds=5)]
                    seen = captures[-1]["state"] if captures else None
        if seen != "normal":
                counts["false_closure"] += 1
                problems.append(f"closed_without_a_normal_capture:{order['order_id']}:{seen}")


def recovery(r: Records) -> dict:
    """Readiness after each fault and the model windows. / 每个故障后的就绪，以及模型故障窗口。"""
    criteria = r.plan["criteria"]
    statuses = r.state.get("occurrences") or {}
    faults, slow = {}, []
    for item in r.occurrences:
        if item["layer"] != "fault":
            continue
        status = statuses.get(item["id"]) or {}
        entry = {k: status.get(k) for k in ("status", "injected", "service_ready_s", "docks_active_s", "robots_fresh_s",
                                            "restored", "restarted")}
        faults[item["id"]] = entry
        if status.get("status") != "done" or status.get("injected") is False:
            slow.append(f"{item['id']}:{status.get('status')}")
            continue
        ready, docks = status.get("service_ready_s"), status.get("docks_active_s")
        if item["kind"] in ("service_restart", "service_kill") and (ready is None or ready > criteria["service_ready_s"]
                                                                    or docks is None
                                                                    or docks - ready > criteria["dock_session_s"]):
            slow.append(f"{item['id']}:service {ready} docks {docks}")
        if item["kind"] in ("fleet_uplink", "s1_uplink") and (status.get("robots_fresh_s") is None
                                                               or status["robots_fresh_s"] > criteria["dock_session_s"]):
            slow.append(f"{item['id']}:robots {status.get('robots_fresh_s')}")
        if item["kind"] in ("vendor_link", "residents_restart") and (docks is None or docks > criteria["dock_session_s"]):
            slow.append(f"{item['id']}:docks {docks}")
    windows = []
    for item in r.occurrences:
        if item["kind"] in ("model_unreachable", "model_throttle"):
            start = _at(item["at"])
            minutes = r.plan["faults"][item["kind"]]["duration_min"]
            windows.append((start, start + timedelta(minutes=minutes)))
    planning = {"inside_failed": 0, "inside_succeeded": 0, "outside_failed": 0, "outside_succeeded": 0}
    for item in r.occurrences:
        if item["kind"] != "planning":
            continue
        status = statuses.get(item["id"]) or {}
        at = _at(status.get("started_at") or item["at"])
        inside = any(a <= at <= b for a, b in windows)
        planned = status.get("planned_status") == "awaiting_approval" or status.get("mission_status") == \
            "awaiting_approval"
        planning[f"{'inside' if inside else 'outside'}_{'succeeded' if planned else 'failed'}"] += 1
    # Inside a window a plan may only fail with a reason; outside, at least nine in ten succeed.
    # 窗口内规划只能带原因失败；窗口外至少九成成功。
    outside = planning["outside_failed"] + planning["outside_succeeded"]
    model_ok = planning["inside_succeeded"] == 0 and (outside == 0 or planning["outside_succeeded"] >= 0.9 * outside)
    ok = not slow and model_ok and bool(faults)
    return {"status": "passed" if ok else "failed", "slow_or_failed": slow[:30], "planning": planning,
            "faults": faults}


def resources(r: Records) -> dict:
    """Restarts beyond the injected ones, out-of-memory kills, burst drain times and growth rates.

    超出注入的重启、内存溢出被杀、积压排空时间与增长率。
    """
    criteria = r.plan["criteria"]
    if not r.samples:
        return {"status": "missing", "reason": "no samples"}
    first, last = r.samples[0].get("residents") or {}, r.samples[-1].get("residents") or {}
    kills = sum(1 for item in r.occurrences if item["kind"] == "service_kill"
                and ((r.state.get("occurrences") or {}).get(item["id"]) or {}).get("injected"))
    unexpected = {}
    for service, start in first.items():
        delta = ((last.get(service) or {}).get("restart_count") or 0) - (start.get("restart_count") or 0)
        allowed = kills if service == "desk-service" else 0
        if delta != allowed:
            unexpected[service] = {"restarts": delta, "injected": allowed}
    oom = sorted({service for sample in r.samples for service, value in (sample.get("residents") or {}).items()
                  if (value or {}).get("oom")})
    high = sorted({name for sample in r.samples for name, value in (sample.get("containers") or {}).items()
                   if _percent((value or {}).get("mem_pct")) >= 95.0})
    bursts = {}
    for item in r.occurrences:
        if item["kind"] != "burst":
            continue
        status = (r.state.get("occurrences") or {}).get(item["id"]) or {}
        started, finished = _at(status.get("started_at")), _at(status.get("finished_at"))
        hours = (finished - started).total_seconds() / 3600 if started and finished else None
        bursts[item["id"]] = {"runs": len(status.get("runs", [])), "expected": status.get("expected"),
                              "drain_h": round(hours, 3) if hours is not None else None}
    late = [k for k, v in bursts.items() if v["drain_h"] is None or v["drain_h"] > criteria["backlog_drain_h"]
            or v["runs"] != v["expected"]]
    span_h = max((_at(r.samples[-1]["at"]) - _at(r.samples[0]["at"])).total_seconds() / 3600, 1e-9)
    ledger0, ledger1 = r.samples[0].get("ledger") or {}, r.samples[-1].get("ledger") or {}
    growth = {"ledger_bytes_per_h": round(((ledger1.get("bytes") or 0) - (ledger0.get("bytes") or 0)) / span_h),
              "media_bytes_per_h": round(((r.samples[-1].get("media_bytes") or 0) -
                                          (r.samples[0].get("media_bytes") or 0)) / span_h),
              "disk_free_start": (r.samples[0].get("host") or {}).get("disk_free_bytes"),
              "disk_free_end": (r.samples[-1].get("host") or {}).get("disk_free_bytes")}
    ok = not unexpected and not oom and not late and bool(bursts)
    return {"status": "passed" if ok else "failed", "unexpected_restarts": unexpected, "oom": oom,
            "memory_over_95pct": high, "bursts": bursts, "late_bursts": late, "growth": growth}


def _percent(value) -> float:
    try:
        return float(str(value).rstrip("%"))
    except (TypeError, ValueError):
        return 0.0


def layers(r: Records) -> dict:
    """Per-layer counts and the minimums. / 分层计数与最低要求。"""
    criteria = r.plan["criteria"]
    soak_missions = r.during(r.tables.get("missions", []), "created_at")
    found: dict[str, Counter] = defaultdict(Counter)
    for mission in soak_missions:
        binding = r.bindings.get(mission["mission_id"]) or {}
        layer = LAYER_OF_PROJECT.get(binding.get("project_id"), "other")
        found[layer]["missions"] += 1
        found[layer][mission["status"]] += 1
    for run in r.during(list(r.runs.values()), "created_at"):
        layer = LAYER_OF_PROJECT.get(run["project_id"], "other")
        found[layer]["runs"] += 1
        found[layer][f"run_{run['state']}"] += 1
    s1_completed = sum(1 for rec in r.flight_records if rec.get("status") == "finished" and
                       (_at(rec.get("started_at")) or r.t0) >= r.t0 and
                       (r.mission_records.get(rec.get("mission_id"), {}).get("judge") or {}).get("passed"))
    s1_judged_failed = sorted(m for m, rec in r.mission_records.items() if (rec.get("judge") or {}).get("passed") is False
                              and any((_at(f.get("started_at")) or r.t0) >= r.t0 for f in rec.get("flights", [])))
    statuses = r.state.get("occurrences") or {}
    probes = [statuses.get(i["id"]) or {} for i in r.occurrences if i["kind"] == "s2_probe"]
    s2_ok = sum(1 for p in probes if p.get("exit_code") == 0)
    vendor_tasks = sum(1 for m in soak_missions
                       if (r.bindings.get(m["mission_id"]) or {}).get("execution_backend") == "vendor_protocol_sim"
                       and m["status"] not in ("declined", "rejected", "refused", "planning_failed"))
    ok = (s1_completed >= criteria["s1_flights_completed"] and vendor_tasks >= criteria["s3_tasks"]
          and s2_ok >= criteria["s2_probes"] and not s1_judged_failed)
    return {"status": "passed" if ok else "failed", "by_layer": {k: dict(v) for k, v in sorted(found.items())},
            "s1_flights_completed_and_judged": s1_completed, "s1_judge_failures": s1_judged_failed[:20],
            "s2_probes": {"planned": len(probes), "succeeded": s2_ok}, "s3_vendor_tasks": vendor_tasks,
            "harness_interventions": sum(1 for a in r.actions if a.get("kind") == "api" and a.get("ok")
                                         and a.get("method") not in ("health",))}


def judge(soak: Path, ledger: Path, *, flights=None, supervisor=None, fleet=None, vendor=None, world=None) -> dict:
    r = Records(soak, ledger, flights=flights, supervisor=supervisor, fleet=fleet, vendor=vendor, world=world)
    counts: Counter = Counter({k: 0 for k in COUNTS})
    criteria = {"duration": duration(r), "schedules": schedules(r, counts), "occurrences": occurrences(r, counts),
                "runs": runs(r, counts), "safety": safety(r, counts), "recovery": recovery(r),
                "resources": resources(r), "layers": layers(r)}
    passed = all(c["status"] == "passed" for c in criteria.values()) and not any(counts.values())
    return {"format": "drone.soak-judge/v1", "soak_id": r.manifest["soak_id"], "source_sha": r.manifest["source_sha"],
            "deployment_id": r.manifest["deployment_id"], "plan_sha256": r.manifest["plan_sha256"],
            "t0": r.manifest["t0"], "end": r.end_at().isoformat(), "passed": passed, "counts": dict(counts),
            "criteria": criteria, "judged_at": datetime.now(timezone.utc).isoformat()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("soak", type=Path)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--flights", type=Path)
    parser.add_argument("--supervisor", type=Path)
    parser.add_argument("--fleet", type=Path)
    parser.add_argument("--vendor", type=Path)
    parser.add_argument("--world", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = judge(args.soak, args.ledger, flights=args.flights, supervisor=args.supervisor, fleet=args.fleet,
                       vendor=args.vendor, world=args.world)
    except Exception as error:  # a judge failure is a failed soak, never a pass / 裁判失败即长稳失败
        result = {"passed": False, "error": f"{type(error).__name__}: {error}"}
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "criteria"}, indent=2, default=str))
    raise SystemExit(0 if result.get("passed") else 1)


if __name__ == "__main__":
    main()
