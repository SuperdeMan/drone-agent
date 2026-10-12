"""The WP-P6-04 asset archive (D087): time text, viewpoints, the asset-key scope, migration and drill, structured
repair, the API parameters and the E4 map fixture. The archive's behaviour under real flows is the S0 matrix
(eval/p6_assets.py, judged by eval/judge_assets.py).

WP-P6-04 资产档案（D087）：时间文本、视角、资产键范围、迁移与演练、结构化维修、接口参数与 E4 地图夹具。档案在真实流程中的
行为由 S0 矩阵验证（eval/p6_assets.py，由 eval/judge_assets.py 裁判）。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from drone_agent.fleet.api import METHODS, OPTIONAL, dispatch
from drone_agent.fleet.asset_store import (
    AUTHORITY,
    INDEXES,
    TABLES,
    ArchiveContext,
    asset_schema,
    context_from_files,
    drill,
    migrate,
    shared_scope,
    stamp,
    viewpoint,
)
from drone_agent.fleet.assets import MODEL_FIELDS, load_compare, open_archive
from drone_agent.fleet.business import BusinessEngine, build_business
from drone_agent.fleet.business_models import Feedback, Repair
from drone_agent.fleet.dispatch import build_operations
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.provenance import ModelUse, RunHeader, seal, source_context
from drone_agent.fleet.resources import load_catalog
from drone_agent.fleet.scheduler import Scheduler, build_scheduling
from drone_agent.fleet.scheduling_models import load_scheduling
from drone_agent.fleet.service import MissionService
from drone_agent.fleet.transport import FleetHub
from drone_agent.fleet.workflow import WorkflowEngine, build_workflows
from drone_agent.mission.registry import M2_SCENE, Registry
from drone_agent.planner.replan import ApprovalPolicy
from drone_agent.runtime.signing import SigningKey

ROOT = Path(__file__).resolve().parents[2]
DESK = {"catalog": "configs/sites/p5_desk_v1.yaml", "members": "configs/sites/p5_members_desk_s0.yaml",
        "workflows": "configs/workflows/p6_desk_v1.yaml", "scheduling": "configs/scheduling/p5_desk_v1.yaml",
        "business": "configs/analysis/p6_desk_v1.yaml"}
DEFINITION = {"frame": {"frame_id": "map_enu", "map_version": "campus_v2"}, "position": [4, 4, 0],
              "route": [[0, 4, 4], [4, 4, 4]]}


def pose(x: float, y: float, z: float = 4.0, map_version: str = "campus_v2") -> dict:
    return {"frame": {"frame_id": "map_enu", "map_version": map_version},
            "position": {"x": x, "y": y, "z": z, "covariance": [0.01] * 9}}


def desk_service(tmp: Path) -> MissionService:
    """A service on the desk catalogs, assembled like the process entry assembles it. / 按进程入口装配的任务台服务。"""
    ledger = BusinessLedger(tmp / "ledger.sqlite3")
    ops = build_operations(ROOT, ledger, ROOT / DESK["catalog"], ROOT / DESK["members"], backups=tmp / "b")
    flows = build_workflows(ROOT, ledger, ROOT / DESK["workflows"], ops, backups=tmp / "b")
    plan = build_scheduling(ledger, ROOT / DESK["scheduling"], ops, backups=tmp / "b")
    trade = build_business(ROOT, ledger, ROOT / DESK["business"], ops, flows, backups=tmp / "b")
    scene = ROOT / M2_SCENE
    service = MissionService(root=ROOT, scene=scene, ledger=ledger, hub=FleetHub(ledger, tmp / "media"),
                             signing_key=SigningKey.generate(),
                             approval_policy=ApprovalPolicy.from_yaml(ROOT / "configs/approval_policy.yaml"),
                             provenance_context=source_context(ROOT, scene, Registry(ROOT, scene=scene).sha256,
                                                               backend="px4_sitl"),
                             operations=ops, backends=("px4_sitl", "logical_sim", "vendor_protocol_sim"))
    service.workflows = WorkflowEngine(service, flows, root=ROOT)
    service.scheduler = Scheduler(service, plan)
    service.business = BusinessEngine(service, trade, root=ROOT, vision=None, recordings=tmp / "rec")
    return service


def test_stamp_makes_text_order_time_order():
    moments = ["2026-10-12T10:00:00Z", "2026-10-12T10:00:00.5+00:00", "2026-10-12T18:00:00.25+08:00",
               datetime(2026, 10, 12, 10, 0, 1, tzinfo=timezone.utc)]
    texts = [stamp(m) for m in moments]
    assert texts[0] == "2026-10-12T10:00:00.000000+00:00"
    assert texts[2] == "2026-10-12T10:00:00.250000+00:00"
    assert sorted(texts) == [texts[0], texts[2], texts[1], texts[3]]
    assert len({len(t) for t in texts}) == 1


def test_viewpoint_is_measured_from_the_pose_and_the_definition():
    above = viewpoint(pose(4, 4), DEFINITION)
    assert above["status"] == "measured" and above["horizontal_m"] == 0 and above["bearing_deg"] is None
    assert above["elevation_deg"] == 90 and above["distance_m"] == 4 and above["declared_heading_deg"] == 0
    east, north = viewpoint(pose(5.2, 4), DEFINITION), viewpoint(pose(4, 5.2), DEFINITION)
    assert (east["bearing_deg"], north["bearing_deg"]) == (0, 90)
    assert east["elevation_deg"] == pytest.approx(73.3, abs=0.01)
    assert viewpoint(pose(4, 4), None) == {"status": "unknown", "reason": "definition_unknown"}
    assert viewpoint(pose(4, 4, map_version="other"), DEFINITION)["reason"] == "frame_mismatch"
    assert viewpoint({"frame": DEFINITION["frame"]}, DEFINITION)["reason"] == "pose_missing"


def test_the_scope_rule_matches_the_business_index():
    desk = load_catalog(ROOT / DESK["catalog"])
    shared = load_scheduling(ROOT / DESK["scheduling"])
    assert {project: shared_scope(desk, shared, project, sites[0]) for project, sites in
            ((p, desk.projects[p].sites) for p in desk.projects)} == dict.fromkeys(desk.projects, "*")
    assert shared_scope(desk, None, "fleet_s0", "site_fa") == "site_fa"
    assert shared_scope(desk, shared, "legacy_m2", "site_x") == "site_x"


async def test_desk_archive_migrates_snapshots_and_drills(tmp_path):
    service = desk_service(tmp_path)
    report = open_archive(service, backups=tmp_path / "b")
    assert report["migration"]["status"] == "migrated" and asset_schema(service.ledger) == "1"
    keys = {r["asset_key"] for r in service.business.archive.store.revisions(project_id="campus_s1")}
    assert keys == {"campus_s1/*/asset_red", "campus_s1/*/asset_blue", "campus_s1/*/road_north"}
    assert report["maps"]["revisions_added"] == 0  # the backfill already took the current maps / 回填已记录当前地图
    listed = await dispatch(service, {"method": "assets.list", "actor": "harness:p5-viewer",
                                      "trust": "first_party", "params": {"project_id": "campus_s1"}})
    assert listed["ok"] and {a["asset_key"] for a in listed["result"]["assets"]} == keys
    events = service.ledger._rows("SELECT kind, authority FROM ar_events")
    assert events and all(e == {"kind": "revision_seen", "authority": "fact"} for e in events)
    service.business.jobs.stop()
    service.ledger.close()
    copy = tmp_path / "copy" / "ledger.sqlite3"
    copy.parent.mkdir()
    shutil.copyfile(tmp_path / "ledger.sqlite3", copy)
    result = drill(copy, context_from_files(ROOT, ROOT / DESK["catalog"], ROOT / DESK["scheduling"]))
    assert result["status"] == "passed" and (result["migration"], result["again"]) == ("current", "current")
    assert result["other_sha256_before"] == result["other_sha256_after"]


def test_a_fresh_copy_migrates_once_and_reports_its_objects(tmp_path):
    service = desk_service(tmp_path)
    service.business.jobs.stop()
    service.ledger.close()
    context = context_from_files(ROOT, ROOT / DESK["catalog"], ROOT / DESK["scheduling"])
    assert isinstance(context, ArchiveContext)
    result = drill(tmp_path / "ledger.sqlite3", context)
    assert result["status"] == "passed" and (result["migration"], result["again"]) == ("migrated", "current")
    assert result["rows"]["ar_revisions"] == result["backfill"]["revisions"] > 0
    assert result["eligible_captures"] == result["rows"]["ar_observations"] == 0
    assert set(TABLES) and len(INDEXES) == 4


def test_feedback_without_a_repair_is_stored_as_before():
    now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    plain = Feedback(feedback_id="fb-1", request_id="r-1", reported_by="harness:op", reported_at=now, note="done")
    assert "repair" not in plain.model_dump(mode="json")
    done = Repair(action="replaced", part="cover", performed_at=now - timedelta(hours=1))
    stored = Feedback(feedback_id="fb-2", request_id="r-2", reported_by="harness:op", reported_at=now, repair=done)
    assert stored.model_dump(mode="json")["repair"]["action"] == "replaced"
    assert Feedback.model_validate(stored.model_dump(mode="json")) == stored
    with pytest.raises(ValueError):
        Feedback(feedback_id="fb-3", request_id="r-3", reported_by="harness:op", reported_at=now,
                 repair=Repair(action="cleaned", performed_at=now + timedelta(seconds=1)))
    with pytest.raises(ValueError):
        Repair(action="painted", performed_at=now)


async def test_only_orders_repair_takes_an_extra_parameter(tmp_path):
    service = desk_service(tmp_path)
    assert OPTIONAL == {"orders.repair": frozenset({"repair"})}
    assert {m for m in METHODS if m.startswith("assets.")} == {"assets.list", "assets.timeline", "assets.compare",
                                                               "assets.maintenance"}
    extra = await dispatch(service, {"method": "assets.list", "actor": "harness:p5-viewer",
                                     "trust": "first_party", "params": {"project_id": "campus_s1", "repair": None}})
    assert extra["issue"]["code"] == "service.invalid_request"
    closed = await dispatch(service, {"method": "assets.list", "actor": "harness:p5-viewer",
                                      "trust": "first_party", "params": {"project_id": "campus_s1"}})
    assert closed["issue"]["code"] == "service.invalid_request"  # the archive is not open yet / 档案尚未打开
    service.business.jobs.stop()
    service.ledger.close()


def test_fact_kinds_and_the_compare_tolerances():
    assert AUTHORITY["finding_opened"] == "belief" and AUTHORITY["round_concluded"] == "decision"
    assert {kind for kind, level in AUTHORITY.items() if level == "fact"} == {
        "finding_confirmed", "finding_dismissed", "order_opened", "repair_reported", "round_reviewed",
        "maintenance_recorded", "reference_registered", "reference_revoked", "revision_seen", "verification_changed"}
    assert {"score", "defect_type", "verdict"} <= MODEL_FIELDS
    config, sha = load_compare(ROOT)
    assert config.version == "compare-v1" and config.bearing_min_horizontal_m == 1.0 and len(sha) == 64


def test_the_e4_map_differs_only_in_the_asset_size():
    base = yaml.safe_load((ROOT / "configs/scenarios/p1_site_b_v1.yaml").read_text(encoding="utf-8"))
    changed = yaml.safe_load((ROOT / "configs/scenarios/p6_site_b_e4_v1.yaml").read_text(encoding="utf-8"))
    assert changed["assets"]["asset_red"].pop("size_m") == 2.2 and base["assets"]["asset_red"].pop("size_m") == 2
    assert changed == base


ASSET = "campus_s1/*/asset_red"
OLD_MAP = "configs/scenarios/m2_campus_v2.yaml"


def seed_capture(service: MissionService, mission_id: str, evidence_id: str, registry_sha: str, at: str,
                 x: float) -> None:
    """One bound, verified capture of campus_s1's asset_red whose sealed run header names `registry_sha`.

    campus_s1 的 asset_red 的一次已绑定、已核验采集，其封存的运行头写明 `registry_sha`。
    """
    header = RunHeader(mission_id=mission_id, mission_version=1, run_id="run-test", execution_backend="px4_sitl",
                       default_image_source="sim_render", scenario_sha256="0" * 64, platform_sha256="0" * 64,
                       registry_sha256=registry_sha, planning=ModelUse(), timestamp=datetime.fromisoformat(at))
    pose = {"frame": {"frame_id": "map_enu", "map_version": "campus_v2"},
            "position": {"x": x, "y": 4.0, "z": 4.0, "covariance": [0.01] * 9}}
    check = {"evidence_id": evidence_id, "mission_id": mission_id, "mission_version": 1, "step_id": "inspect",
             "final_verdict": "verified", "service_verdict": "verified", "onboard_verdict": "verified",
             "agrees": True, "checks": {"media": "ok"}, "verified_at": at}
    with service.ledger.transaction() as db:
        db.execute("INSERT INTO missions(mission_id, request_id, status, created_at, updated_at) VALUES (?,?,?,?,?)",
                   (mission_id, "req-" + mission_id, "completed", at, at))
        db.execute("INSERT INTO versions(mission_id, version, status, origin, package, decision, created_at, "
                   "updated_at) VALUES (?,?,?,?,?,?,?,?)",
                   (mission_id, 1, "finished", "console",
                    json.dumps({"nodes": [{"task_id": "inspect", "params": {"asset_id": "asset_red"}}]}),
                    json.dumps({"run_provenance": {"run": seal(header)}}), at, at))
        db.execute("INSERT INTO op_bindings VALUES (?,?,?,?,?,?,?,?)",
                   (mission_id, "campus_s1", "site_s1", "dock_s1", "uav_01", "px4_sitl", "c" * 64, at))
        db.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (evidence_id, "uav_01", mission_id, 1, "inspect", "e" * 64, None, None, None, None,
                    json.dumps({"evidence_id": evidence_id, "time_window": {"timestamp": at}, "captured_pose": pose,
                                "subject_ids": ["asset_red"]}), at))
        db.execute("INSERT INTO verifications VALUES (?,?,?,?,?,?,?,?,?)",
                   (evidence_id, mission_id, 1, "inspect", "verified", "verified", 1, json.dumps(check), at))


async def test_a_map_no_longer_current_is_recovered_by_its_digest(tmp_path):
    service = desk_service(tmp_path)
    old, current = Registry(ROOT, scene=ROOT / OLD_MAP).sha256, service.ops.registries["site_s1"].sha256
    assert old != current
    seed_capture(service, "m-000000000001", "capture:old", old, "2026-09-28T01:42:02+00:00", 3.2)
    seed_capture(service, "m-000000000002", "capture:new", current, "2026-10-10T13:28:38+00:00", 3.3)
    report = open_archive(service, backups=tmp_path / "b")
    assert report["migration"]["status"] == "migrated" and report["maps"]["recovered"] == 0
    store = service.business.archive.store
    recovered = store.revision(ASSET, old)
    assert recovered["definition"]["recovered_from"] == OLD_MAP
    assert recovered["first_seen_at"] == stamp("2026-09-28T01:42:02+00:00")
    assert store.observation("capture:old")["geometry"]["status"] == "measured"
    answer = await dispatch(service, {"method": "assets.compare", "actor": "harness:p5-viewer", "trust": "first_party",
                                      "params": {"project_id": "campus_s1", "asset": ASSET, "evidence": None,
                                                 "against": None}})
    factors = answer["result"]["factors"]
    assert answer["result"]["against"]["evidence_id"] == "capture:old"
    assert factors["definition"]["status"] == "unchanged"  # a new map, the same asset / 新地图，资产不变
    assert factors["viewpoint"]["status"] == "consistent"
    service.business.jobs.stop()
    service.ledger.close()


async def test_a_ledger_migrated_earlier_recovers_the_map_and_measures_on_read(tmp_path):
    service = desk_service(tmp_path)
    old = Registry(ROOT, scene=ROOT / OLD_MAP).sha256
    seed_capture(service, "m-000000000001", "capture:old", old, "2026-09-28T01:42:02+00:00", 3.2)
    # Migrated by a build that did not look in the repository. / 由不查找仓库地图的版本迁移。
    migrate(service.ledger, ArchiveContext(service.ops.catalog, service.scheduler.catalog, service.ops.registries),
            backups=tmp_path / "b")
    stored = json.loads(service.ledger._one("SELECT geometry FROM ar_observations")["geometry"])
    assert stored == {"status": "unknown", "reason": "definition_unknown"}
    report = open_archive(service, backups=tmp_path / "b")
    assert report["migration"]["status"] == "current" and report["maps"]["recovered"] == 2  # asset_red, asset_blue
    assert json.loads(service.ledger._one("SELECT geometry FROM ar_observations")["geometry"]) == stored
    answer = await dispatch(service, {"method": "assets.timeline", "actor": "harness:p5-viewer", "trust": "first_party",
                                      "params": {"project_id": "campus_s1", "asset": ASSET, "before": None,
                                                 "limit": 20}})
    shown = next(i for i in answer["result"]["columns"]["fact"] if i["kind"] == "observation")["body"]["geometry"]
    assert shown["status"] == "measured" and shown["definition_recovered"] is True
    assert shown["horizontal_m"] == pytest.approx(0.8)
    service.business.jobs.stop()
    service.ledger.close()
