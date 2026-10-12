"""Independent judge for one asset archive case (WP-P6-04, D087): every P6 check plus the archive invariants.

The judge never reuses the archive, the business engine or the service. From the final ledger, the exported tables,
the injections and the recorded API answers it re-derives that: the observations are exactly the verified captures
of an inspected asset, each under the asset key of its binding; every business transition (suspected result joining
a finding, review, order, repair feedback, settlement, reference, maintenance) is present as one event whose
authority matches its kind; no model field reaches the fact column of a timeline answer or a comparison factor; no
comparison summary names a cause; and each answer agrees with the world's truth: the viewpoint the judge recomputes
from the poses and the site map, the injected drifts, the repair the operator reported, the map change and the
rewritten judgement. E6 copies are re-read on their own: identical archives at one instant, nothing else changed,
and every backfilled business event equal to the one written live.

单个资产档案用例的独立裁判（WP-P6-04，D087）：P6 的全部检查加上档案不变量。

裁判从不复用档案、业务引擎或服务。它从最终账本、导出表、注入与记录的 API 回答重新推导：观测恰好是被检资产的已核验采集，
各自位于其绑定所给的资产键下；每次业务转移（疑似结果并入发现、复核、工单、维修反馈、结算、参考图、维护）都恰有一条事件，
且权威等级与其种类相符；模型字段不进入任何时间线回答的事实栏或比较因素；比较摘要不指明原因；每个回答都与世界真值一致：
裁判按位姿与站点地图复算的视角、注入的拍摄偏差、操作者报告的维修、地图变更与被改写的判断。E6 的副本单独重读：同一时刻的
档案相同、其余不变，回填的每条业务事件都与在线写入的相同。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

from drone_agent.eval.judge_p4 import _json, business_counts, verdict
from drone_agent.eval.judge_p6 import COUNTS as P6_COUNTS
from drone_agent.eval.judge_p6 import Case, check_expected_p6, metrics, p6_counts

COUNTS = (*P6_COUNTS, "archive_observation_mismatch", "archive_event_mismatch", "archive_authority_mismatch",
          "archive_model_field", "archive_attribution")
AUTHORITY = {"finding_opened": "belief", "round_concluded": "decision"}
FACT_KINDS = ("finding_confirmed", "finding_dismissed", "order_opened", "repair_reported", "round_reviewed",
              "maintenance_recorded", "reference_registered", "reference_revoked", "revision_seen",
              "verification_changed")
MODEL_FIELDS = {"score", "defect_type", "family", "verdict", "job_verdict", "anomaly_score", "model_id",
                "reported_model_id", "description"}
CAUSES_ZH = ("因为", "由于", "导致", "归因", "所以", "是维修", "是角度", "是新异常", "属于维修", "属于新异常")
CAUSES_EN = ("because", "due to", "caused", "attribut", "therefore", "is a repair", "new anomaly", "is the angle")
SITE_MAP = "configs/scenarios/p1_site_b_v1.yaml"


def _utc(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _rows(path: Path, sql: str) -> list[dict]:
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute(sql)]
    finally:
        db.close()


def _keys(value) -> set[str]:
    found: set[str] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            found |= set(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


class Archive:
    """The archive and the business records of one ledger file. / 一个账本文件中的档案与业务记录。"""

    def __init__(self, path: Path):
        self.path = path
        self.observations = {r["evidence_id"]: {**r, "geometry": _json(r["geometry"]),
                                                "verification": _json(r["verification"])}
                             for r in _rows(path, "SELECT * FROM ar_observations")}
        self.events = {r["event_id"]: {**r, "refs": _json(r["refs"]), "body": _json(r["body"])}
                       for r in _rows(path, "SELECT * FROM ar_events")}
        self.revisions = _rows(path, "SELECT * FROM ar_revisions")

    def by_source(self) -> dict[str, dict]:
        return {e["source_key"]: e for e in self.events.values()}


def eligible(path: Path) -> dict[str, tuple[str, str, str]]:
    """evidence id -> (project, site, asset) for every verified capture of an inspected asset in a bound mission.

    已绑定任务中被检资产的每次已核验采集：证据 id -> （项目，站点，资产）。
    """
    bindings = {r["mission_id"]: r for r in _rows(path, "SELECT mission_id, project_id, site_id FROM op_bindings")}
    steps: dict[tuple[str, int], dict[str, str]] = {}
    for row in _rows(path, "SELECT mission_id, version, package FROM versions"):
        nodes = (_json(row["package"]) or {}).get("nodes", [])
        steps[(row["mission_id"], row["version"])] = {n["task_id"]: n["params"]["asset_id"] for n in nodes
                                                      if "asset_id" in (n.get("params") or {})}
    found = {}
    for row in _rows(path, "SELECT e.evidence_id, e.mission_id, e.mission_version, e.step_id FROM evidence e "
                           "JOIN verifications v ON v.evidence_id = e.evidence_id"):
        asset = steps.get((row["mission_id"], row["mission_version"]), {}).get(row["step_id"])
        binding = bindings.get(row["mission_id"])
        if asset is not None and binding is not None:
            found[row["evidence_id"]] = (binding["project_id"], binding["site_id"], asset)
    return found


def expected_sources(path: Path) -> dict[str, str]:
    """source key -> event kind every business row implies. / 每条业务行所意味的来源键 -> 事件种类。"""
    found = {}
    for row in _rows(path, "SELECT job_id FROM bz_jobs WHERE finding_id IS NOT NULL"):
        found[f"job:{row['job_id']}"] = "finding_opened"
    for row in _rows(path, "SELECT review_id, subject_kind, decision FROM bz_reviews"):
        found[f"review:{row['review_id']}"] = "round_reviewed" if row["subject_kind"] == "round" else (
            "finding_confirmed" if row["decision"] == "confirmed" else "finding_dismissed")
    for row in _rows(path, "SELECT order_id FROM bz_orders"):
        found[f"order:{row['order_id']}"] = "order_opened"
    for row in _rows(path, "SELECT order_id, round, feedback, conclusion FROM bz_rounds"):
        found[f"feedback:{_json(row['feedback'])['feedback_id']}"] = "repair_reported"
        if row["conclusion"] is not None:
            found[f"round:{row['order_id']}:r{row['round']}"] = "round_concluded"
    for row in _rows(path, "SELECT reference_id, revoked_at FROM bz_references"):
        found[f"reference:{row['reference_id']}:registered"] = "reference_registered"
        if row["revoked_at"] is not None:
            found[f"reference:{row['reference_id']}:revoked"] = "reference_revoked"
    return found


def check_invariants(path: Path, problems: list[str]) -> dict[str, int]:
    archive = Archive(path)
    wanted = eligible(path)
    observation = 0
    for evidence_id in sorted(set(wanted) ^ set(archive.observations)):
        observation += 1
        problems.append(f"observation_{'missing' if evidence_id in wanted else 'extra'}:{evidence_id[:40]}")
    for evidence_id, (project, site, asset) in wanted.items():
        found = archive.observations.get(evidence_id)
        if found is not None and found["asset_key"] != f"{project}/{site}/{asset}":
            observation += 1
            problems.append(f"observation_asset_key:{evidence_id[:40]}:{found['asset_key']}")
    events = 0
    sources = archive.by_source()
    for key, kind in expected_sources(path).items():
        if key not in sources or sources[key]["kind"] != kind:
            events += 1
            problems.append(f"event_missing:{key}:{kind}")
    counted = [e["source_key"] for e in archive.events.values()]
    if len(counted) != len(set(counted)):
        events += 1
        problems.append("event_source_duplicated")
    authority = 0
    for item in archive.events.values():
        expected = AUTHORITY.get(item["kind"], "fact" if item["kind"] in FACT_KINDS else None)
        if expected is None or item["authority"] != expected:
            authority += 1
            problems.append(f"event_authority:{item['kind']}:{item['authority']}")
    return {"archive_observation_mismatch": observation, "archive_event_mismatch": events,
            "archive_authority_mismatch": authority}


def check_answers(answers: list[dict], problems: list[str]) -> dict[str, int]:
    """No model field in a fact column or a factor; no summary naming a cause. / 事实栏与因素中无模型字段；摘要不指明原因。"""
    model = causes = 0
    for answer in answers:
        response = answer["response"]
        if not response.get("ok"):
            problems.append(f"answer_refused:{answer['label']}:{(response.get('issue') or {}).get('code')}")
            continue
        result = response["result"]
        if answer["method"] == "assets.timeline":
            leaked = _keys(result["columns"]["fact"]) & MODEL_FIELDS
            if leaked:
                model += 1
                problems.append(f"model_field_in_fact_column:{answer['label']}:{sorted(leaked)}")
        if answer["method"] == "assets.compare":
            leaked = _keys(result["factors"]) & MODEL_FIELDS
            if leaked:
                model += 1
                problems.append(f"model_field_in_factor:{answer['label']}:{sorted(leaked)}")
            summary = result.get("summary") or {}
            if any(word in summary.get("zh", "") for word in CAUSES_ZH) or \
                    any(word in summary.get("en", "").lower() for word in CAUSES_EN):
                causes += 1
                problems.append(f"summary_names_a_cause:{answer['label']}")
    return {"archive_model_field": model, "archive_attribution": causes}


# ── truth / 真值 ──


def viewpoint(pose: dict, asset: list[float]) -> dict:
    """The judge's own viewpoint of a capture. / 裁判自己的采集视角。"""
    p = pose["position"]
    dx, dy, dz = p["x"] - asset[0], p["y"] - asset[1], p["z"] - asset[2]
    horizontal = math.hypot(dx, dy)
    return {"distance": math.hypot(horizontal, dz), "horizontal": horizontal,
            "bearing": math.degrees(math.atan2(dy, dx)) % 360 if horizontal > 1e-9 else None,
            "elevation": math.degrees(math.atan2(dz, horizontal))}


def _answer(answers: list[dict], label: str) -> dict | None:
    found = next((a for a in answers if a["label"] == label), None)
    return found["response"].get("result") if found and found["response"].get("ok") else None


def _latest(beliefs: list[dict]) -> str | None:
    done = [b for b in beliefs if b["state"] in ("completed", "refused")]
    return done[-1]["verdict"] if done else None


def check_compare(c, root: Path, label: str, result: dict | None, wanted: dict, problems: list[str],
                  captures: dict) -> None:
    if result is None:
        problems.append(f"compare_missing:{label}")
        return
    factors = result["factors"]
    if "repairs" in wanted and factors["events"]["repairs"] != wanted["repairs"]:
        problems.append(f"{label}:repairs {factors['events']['repairs']} != {wanted['repairs']}")
    if "viewpoint" in wanted and factors["viewpoint"]["status"] != wanted["viewpoint"]:
        problems.append(f"{label}:viewpoint {factors['viewpoint']['status']} != {wanted['viewpoint']}")
    if "exceeded" in wanted and factors["viewpoint"].get("exceeded") != wanted["exceeded"]:
        problems.append(f"{label}:exceeded {factors['viewpoint'].get('exceeded')} != {wanted['exceeded']}")
    if "definition" in wanted and factors["definition"]["status"] != wanted["definition"]:
        problems.append(f"{label}:definition {factors['definition']['status']} != {wanted['definition']}")
    if "changed_fields" in wanted and factors["definition"].get("changed_fields") != wanted["changed_fields"]:
        problems.append(f"{label}:changed_fields {factors['definition'].get('changed_fields')}")
    if "role" in wanted and result["against"].get("role") != wanted["role"]:
        problems.append(f"{label}:role {result['against'].get('role')} != {wanted['role']}")
    for side, verdict_ in (wanted.get("beliefs") or {}).items():
        if _latest(result["beliefs"][side]) != verdict_:
            problems.append(f"{label}:belief {side} {_latest(result['beliefs'][side])} != {verdict_}")
    for side in ("evidence", "against"):
        if wanted.get(side) and captures.get(wanted[side]) != result[side]["evidence_id"]:
            problems.append(f"{label}:{side} is not the {wanted[side]} capture")
    # The viewpoint the judge recomputes from the poses and the map. / 裁判按位姿与地图复算的视角。
    measures = factors["viewpoint"].get("measures")
    if measures is not None:
        site = yaml.safe_load((root / SITE_MAP).read_text(encoding="utf-8"))
        asset = site["assets"]["asset_red"]["position"]
        mine = [viewpoint(c.evidence[result[side]["evidence_id"]]["body"]["captured_pose"], asset)
                for side in ("evidence", "against")]
        if abs(measures["elevation_diff_deg"] - abs(mine[0]["elevation"] - mine[1]["elevation"])) > 0.05:
            problems.append(f"{label}:elevation_diff {measures['elevation_diff_deg']} disagrees with the poses")
        if abs(measures["distance_ratio"] - mine[0]["distance"] / mine[1]["distance"]) > 0.001:
            problems.append(f"{label}:distance_ratio {measures['distance_ratio']} disagrees with the poses")
        both = min(mine[0]["horizontal"], mine[1]["horizontal"]) >= 1.0
        if both:
            difference = abs(mine[0]["bearing"] - mine[1]["bearing"]) % 360
            if measures["bearing_diff_deg"] is None or \
                    abs(measures["bearing_diff_deg"] - min(difference, 360 - difference)) > 0.05:
                problems.append(f"{label}:bearing_diff {measures['bearing_diff_deg']} disagrees with the poses")
        elif measures["bearing_diff_deg"] is not None:
            problems.append(f"{label}:bearing compared right above the asset")
        if "offset_diff_side" in measures and result["evidence"]["registry_sha256"] ==                 result["against"]["registry_sha256"]:
            size = site["assets"]["asset_red"]["size_m"]
            if abs(measures["offset_diff_side"] - abs(mine[0]["horizontal"] - mine[1]["horizontal"]) / size) > 0.01:
                problems.append(f"{label}:offset_diff_side {measures['offset_diff_side']} disagrees with the poses")


def check_drift(c, captures: dict, problems: list[str]) -> None:
    """Each drifted capture's pose is the nadir pose moved by the injected drift. / 偏差采集的位姿等于正上方位姿加注入偏差。"""
    base = c.evidence[captures["nadir"]]["body"]["captured_pose"]["position"]
    for label, offset in (("east", (1.2, 0.0)), ("north", (0.0, 1.2))):
        pose = c.evidence[captures[label]]["body"]["captured_pose"]["position"]
        if abs(pose["x"] - base["x"] - offset[0]) > 0.05 or abs(pose["y"] - base["y"] - offset[1]) > 0.05:
            problems.append(f"drift_truth:{label} pose does not carry the injected drift")


def check_isolation(answers: list[dict], c, problems: list[str]) -> None:
    before, after = _answer(answers, "compare_before"), _answer(answers, "compare_after")
    line_before, line_after = _answer(answers, "timeline_before"), _answer(answers, "timeline_after")
    if None in (before, after, line_before, line_after):
        problems.append("isolation_answers_missing")
        return
    if before["factors"] != after["factors"]:
        problems.append("isolation:factors changed with a model judgement")
    if line_before["columns"]["fact"] != line_after["columns"]["fact"] or \
            line_before["columns"]["decision"] != line_after["columns"]["decision"]:
        problems.append("isolation:fact or decision column changed with a model judgement")
    rewritten = next((i["job_id"] for i in c.injections if i["kind"] == "judgement_rewritten"), None)
    shown = [item for item in line_after["columns"]["belief"]
             if item["kind"] == "analysis" and item["refs"]["job_id"] == rewritten]
    if not shown or shown[0]["body"]["verdict"] != "normal" or \
            line_before["columns"]["belief"] == line_after["columns"]["belief"]:
        problems.append("isolation:the belief column does not show the rewritten judgement")


def _digest_other(path: Path) -> str:
    """The judge's own digest of everything outside the archive. / 裁判自己对档案以外全部内容的摘要。"""
    digest = hashlib.sha256()
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        for name, sql in db.execute("SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'ar\\_%' ESCAPE '\\' "
                                    "AND tbl_name NOT LIKE 'ar\\_%' ESCAPE '\\' ORDER BY name"):
            digest.update(f"{name}\x00{sql}\x00".encode())
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'ar\\_%' "
                                   "ESCAPE '\\' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            rows = sorted(repr(tuple(r)) for r in db.execute(f'SELECT * FROM "{table}"')
                          if not (table == "meta" and str(r[0]).startswith("asset_")))
            digest.update(f"{table}\x00".encode() + "\x00".join(rows).encode())
    finally:
        db.close()
    return digest.hexdigest()


def _digest_archive(path: Path) -> str:
    digest = hashlib.sha256()
    for table in ("ar_revisions", "ar_observations", "ar_events"):
        rows = sorted(repr(tuple(r.values())) for r in _rows(path, f"SELECT * FROM {table}"))
        digest.update(f"{table}\x00".encode() + "\x00".join(rows).encode())
    return digest.hexdigest()


def check_backfill(case: Path, c, problems: list[str]) -> None:
    folder = case / "e6"
    injected = next((i for i in c.injections if i["kind"] == "backfill_copies"), None)
    if injected is None or not all((folder / f"{n}.sqlite3").is_file() for n in ("live", "stripped", "a", "b")):
        problems.append("backfill_copies_missing")
        return
    if injected["migrations"] != {"a": ["migrated", "current"], "b": ["migrated"]}:
        problems.append(f"backfill_migrations {injected['migrations']}")
    if _digest_archive(folder / "a.sqlite3") != _digest_archive(folder / "b.sqlite3"):
        problems.append("backfill_not_deterministic")
    others = {n: _digest_other(folder / f"{n}.sqlite3") for n in ("stripped", "a", "b")}
    if len(set(others.values())) != 1:
        problems.append("backfill_changed_other_objects")
    filled, live = Archive(folder / "a.sqlite3"), Archive(folder / "live.sqlite3")
    if set(filled.observations) != set(eligible(folder / "a.sqlite3")) or set(filled.observations) != set(
            live.observations):
        problems.append("backfill_observations_incomplete")
    for evidence_id, row in filled.observations.items():
        other = live.observations.get(evidence_id) or {}
        same = ("asset_key", "mission_id", "mission_version", "step_id", "registry_sha256", "captured_at",
                "media_sha256", "geometry", "recapture_of")
        if any(row[k] != other.get(k) for k in same) or \
                row["verification"]["final_verdict"] != (other.get("verification") or {}).get("final_verdict"):
            problems.append(f"backfill_observation_differs:{evidence_id[:40]}")
    sources = live.by_source()
    for key, item in filled.by_source().items():
        if item["kind"] in ("revision_seen", "maintenance_recorded"):
            continue  # first seen at the backfill; maintenance has no business table / 回填时首见；维护没有业务表
        other = sources.get(key)
        if other is None or any(item[k] != other[k] for k in ("event_id", "kind", "authority", "at", "actor",
                                                               "asset_key", "refs", "body")):
            problems.append(f"backfill_event_differs:{key}")
    missing = sorted(k for k, v in sources.items() if k not in filled.by_source()
                     and v["kind"] not in ("maintenance_recorded", "verification_changed"))
    if missing:
        problems.append(f"backfill_events_missing:{missing[:5]}")


def judge_case(case: Path, root: Path, *, use_replay: bool = False) -> dict:
    from drone_agent.eval.judge_p1 import check_flights

    c = Case(case, root)
    problems: list[str] = []
    flight_false = check_flights(c, problems, use_replay=use_replay)
    counts = p6_counts(c, problems, business_counts(c, problems, use_replay=use_replay, flight_false=flight_false))
    found = metrics(c)
    check_expected_p6(c, problems, found)
    exported = json.loads((case / "service-export/assets.json").read_text(encoding="utf-8"))
    answers = exported["answers"]
    counts.update(check_invariants(case / "service/ledger.sqlite3", problems))
    counts.update(check_answers(answers, problems))
    expected = c.scenario.get("expected", {})
    for kind in expected.get("injections", []):
        if not any(i["kind"] == kind for i in c.injections):
            problems.append(f"injection_missing:{kind}")
    wanted = expected.get("archive", {})
    captures = next((i for i in reversed(c.injections) if i["kind"] == "captures"), {})
    if c.scenario["scenario"] == "p6_e1_repair":
        captures = e1_captures(c)
        check_repair(c, _answer(answers, "compare"), problems)
    for label, spec in wanted.items():
        if label in ("isolation", "backfill"):
            continue
        check_compare(c, root, label, _answer(answers, label), spec, problems, captures)
    if c.scenario["scenario"] == "p6_e2_angle" and captures:
        check_drift(c, captures, problems)
    if wanted.get("isolation"):
        check_isolation(answers, c, problems)
    if wanted.get("backfill"):
        check_backfill(case, c, problems)
    archive = Archive(case / "service/ledger.sqlite3")
    return verdict(c, problems, counts, case, layer="S0", use_replay=use_replay, metrics=found,
                   archive={"observations": len(archive.observations), "events": len(archive.events),
                            "revisions": len(archive.revisions), "answers": len(answers)})


def e1_captures(c) -> dict:
    """The inspection and reinspection captures of E1, from the runs' node outputs. / E1 的巡检与复检采集。"""
    found = {}
    for run_id, run in c.runs.items():
        result = (c.nodes.get(run_id, {}).get("await_inspection") or {}).get("result")
        result = _json(result) if isinstance(result, str) else result
        if result:
            found["reinspection" if run["workflow_id"].endswith("reinspection") else "inspection"] = \
                result["evidence_id"]
    return found


def check_repair(c, result: dict | None, problems: list[str]) -> None:
    """The comparison lists the repair the operator reported, as reported. / 比较列出操作者报告的维修，与报告一致。"""
    reported = next((i["repair"] for i in c.injections if i["kind"] == "repair_record"), None)
    items = [i for i in ((result or {}).get("factors") or {}).get("events", {}).get("items", [])
             if i["kind"] == "repair_reported"]
    if reported is None or len(items) != 1:
        problems.append("repair_not_listed")
        return
    shown = items[0]["body"].get("repair") or {}
    if shown.get("action") != reported["action"] or shown.get("part") != reported["part"] or \
            _utc(shown.get("performed_at", "1970-01-01T00:00:00+00:00")) != _utc(reported["performed_at"]):
        problems.append(f"repair_differs:{shown}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    args = parser.parse_args()
    print(json.dumps(judge_case(args.case, args.root), indent=2, default=str))


if __name__ == "__main__":
    main()
