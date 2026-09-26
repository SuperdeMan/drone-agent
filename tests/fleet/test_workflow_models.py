"""WP-P2-01: workflow templates are typed, acyclic, bounded and confined to their project; schedules are explicit.

WP-P2-01：工作流模板有类型、无环、有界且限于本项目；排班语义明确。
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from drone_agent.fleet.resources import load_catalog
from drone_agent.fleet.workflow_models import (
    DailySchedule,
    IntervalSchedule,
    ScheduleTrigger,
    WorkflowCatalog,
    due_occurrences,
    load_workflows,
    next_occurrence,
    zone,
)
from drone_agent.mission.registry import Registry

ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc


def pairs():
    return (("configs/workflows/p2_s1_v1.yaml", "configs/sites/p1_s1_v1.yaml"),
            ("configs/workflows/p2_campus_v1.yaml", "configs/sites/p1_campus_v1.yaml"))


def registries(operations):
    return {site_id: Registry(ROOT, scene=ROOT / site.scene) for site_id, site in operations.sites.items()}


def raw(path: str = "configs/workflows/p2_campus_v1.yaml") -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def workflow(data: dict, workflow_id: str) -> dict:
    return next(w for w in data["workflows"] if w["workflow_id"] == workflow_id)


def node(spec: dict, node_id: str) -> dict:
    return next(n for n in spec["nodes"] if n["node_id"] == node_id)


def checked(data: dict, operations: str = "configs/sites/p1_campus_v1.yaml") -> WorkflowCatalog:
    catalog = WorkflowCatalog.model_validate(data)
    ops = load_catalog(ROOT / operations)
    catalog.check(ops, registries(ops), ROOT)
    return catalog


@pytest.mark.parametrize("workflows, operations", pairs())
def test_the_repository_catalogs_load_and_match_their_operations_catalog(workflows, operations):
    catalog = load_workflows(ROOT / workflows)
    ops = load_catalog(ROOT / operations)
    fixtures = catalog.check(ops, registries(ops), ROOT)
    assert set(fixtures) == {"scripted_fixture_v1"} and len(fixtures["scripted_fixture_v1"]) == 64
    for spec in catalog.workflows:
        order = spec.order()
        for node_id in order:
            assert spec.dependencies(node_id) <= set(order[:order.index(node_id)])
    other = load_catalog(ROOT / next(o for w, o in pairs() if w != workflows))
    with pytest.raises(ValueError, match="written for"):
        catalog.check(other, registries(other), ROOT)


def test_a_cycle_is_refused():
    data = raw()
    spec = workflow(data, "campus_round")
    node(spec, "inspect_red")["after"] = ["report"]
    with pytest.raises(ValidationError, match="cycle"):
        WorkflowCatalog.model_validate(data)


@pytest.mark.parametrize("mutate, message", [
    (lambda s: node(s, "analyze_red").update(activity="run_python"), "does not match any of the expected tags"),
    (lambda s: node(s, "analyze_red")["params"].update(script="import os"), "Extra inputs are not permitted"),
    (lambda s: node(s, "review_red").update(when="analyze_red.suspected == True"), "valid tuple"),
    (lambda s: node(s, "review_red").update(when=[{"node": "analyze_red", "output": "__class__", "equals": True}]),
     "Input should be 'suspected' or 'decision'"),
    (lambda s: node(s, "inspect_blue")["params"].update(url="https://example.test/hook"), "Extra inputs"),
    (lambda s: node(s, "await_red")["params"].update(mission_from="nowhere"), "must read a submit_mission node"),
    (lambda s: node(s, "report").update(after=["ghost"]), "unknown node ghost"),
    (lambda s: node(s, "order_red").update(when=[]), "must require a confirmed decision"),
    (lambda s: node(s, "review_red").update(when=[{"node": "analyze_red", "output": "decision", "equals": "confirmed"}]),
     "cannot produce"),
    (lambda s: node(s, "review_red").update(when=[{"node": "analyze_red", "output": "suspected", "equals": "yes"}]),
     "valid boolean"),
    (lambda s: s["nodes"].append({"node_id": "inspect_extra", "activity": "submit_mission", "params": {
        "robot_id": "uav_a", "volume_id": "campus_training", "asset": "asset_red"}}), "exactly one await_mission"),
    (lambda s: s.update(budget={"max_missions": 1}), "exceed the budget"),
    (lambda s: s["nodes"].append({"node_id": "loop", "activity": "submit_mission", "after": ["loop"],
                                  "params": {"robot_id": "uav_a", "volume_id": "campus_training",
                                             "asset": "asset_red"}}), "unknown node loop"),
], ids=["unknown-activity", "free-code-param", "expression-condition", "dunder-output", "url-param",
        "wrong-reference", "unknown-after", "order-without-review", "wrong-output", "wrong-value", "unawaited",
        "budget", "self-loop"])
def test_templates_reject_unknown_nodes_free_code_and_ungrounded_steps(mutate, message):
    data = raw()
    mutate(workflow(data, "campus_round"))
    with pytest.raises(ValidationError, match=message):
        WorkflowCatalog.model_validate(data)


def test_a_condition_must_read_an_ancestor():
    data = raw()
    spec = workflow(data, "campus_round")
    node(spec, "review_red")["when"] = [{"node": "analyze_blue", "output": "suspected", "equals": True}]
    with pytest.raises(ValidationError, match="not an ancestor"):
        WorkflowCatalog.model_validate(data)


def test_reinspection_never_chains_and_needs_an_internal_target():
    data = raw()
    target = workflow(data, "asset_reinspection")
    target["triggers"] = [{"trigger_id": "manual", "kind": "manual"}]
    with pytest.raises(ValidationError, match="no internal trigger"):
        WorkflowCatalog.model_validate(data)
    data = raw()
    chained = copy.deepcopy(workflow(data, "asset_check"))
    chained.update(workflow_id="asset_reinspection", version=2,
                   triggers=[{"trigger_id": "reinspection", "kind": "internal"}])
    data["workflows"].append(chained)
    node(workflow(data, "asset_check"), "reinspect")["params"]["version"] = 2
    with pytest.raises(ValidationError, match="may not itself request reinspection"):
        WorkflowCatalog.model_validate(data)


@pytest.mark.parametrize("mutate, message", [
    (lambda s: node(s, "inspect")["params"].update(robot_id="uav_h"), "robot outside its project"),
    (lambda s: node(s, "inspect")["params"].update(volume_id="harbor_volume"), "unregistered volume"),
    (lambda s: s["inputs"]["asset"].update(choices=["asset_red", "asset_green"]), "asset_green, not a registered"),
    (lambda s: node(s, "inspect")["params"].update(asset="asset_green"), "asset_green, not a registered"),
    (lambda s: s.update(project_id="harbor_ops"), "robot outside its project"),
    (lambda s: s.update(project_id="legacy_m2"), "unknown project"),
], ids=["foreign-robot", "unknown-volume", "unknown-choice", "unknown-asset", "moved-project", "legacy-project"])
def test_the_catalog_check_confines_nodes_to_their_project_and_registry(mutate, message):
    data = raw()
    mutate(workflow(data, "quick_check"))
    with pytest.raises(ValueError, match=message):
        checked(data)


def test_a_fixture_outside_the_repository_is_refused():
    data = raw()
    data["analyzers"]["scripted_fixture_v1"]["fixture"] = "../outside.yaml"
    with pytest.raises(ValueError, match="outside the repository"):
        checked(data)


def test_triggers_must_supply_exactly_the_inputs():
    data = raw()
    spec = workflow(data, "asset_check")
    next(t for t in spec["triggers"] if t["kind"] == "schedule")["inputs"] = {"asset": "asset_green"}
    with pytest.raises(ValidationError, match="not one of its choices"):
        WorkflowCatalog.model_validate(data)
    data = raw()
    next(t for t in workflow(data, "asset_check")["triggers"] if t["kind"] == "event")["inputs_from_event"] = []
    with pytest.raises(ValidationError, match="every required input"):
        WorkflowCatalog.model_validate(data)
    data = raw()
    next(t for t in workflow(data, "asset_check")["triggers"] if t["kind"] == "event")["source"] = "tailnet:someone"
    with pytest.raises(ValidationError, match="event"):
        WorkflowCatalog.model_validate(data)


def test_run_inputs_are_checked_against_declared_choices():
    spec = load_workflows(ROOT / "configs/workflows/p2_campus_v1.yaml").latest("campus_ops", "asset_check")
    assert spec.check_inputs({"asset": "asset_blue"}) == {"asset": "asset_blue"}
    for values in ({}, {"asset": "asset_green"}, {"asset": "asset_red", "robot": "uav_h"}, {"asset": ["asset_red"]}):
        with pytest.raises(ValueError):
            spec.check_inputs(values)


def test_versions_are_immutable_entries_and_new_runs_take_the_latest():
    data = raw()
    newer = copy.deepcopy(workflow(data, "quick_check"))
    newer.update(version=2, title="Site C quick check v2 / C 站快速检查 v2")
    data["workflows"].append(newer)
    catalog = checked(data)
    assert catalog.latest("campus_ops", "quick_check").version == 2
    assert catalog.spec("campus_ops", "quick_check", 1).title.startswith("Site C quick check:")
    assert catalog.spec("campus_ops", "quick_check", 1).sha256 != catalog.spec("campus_ops", "quick_check", 2).sha256
    data["workflows"].append(copy.deepcopy(newer))
    with pytest.raises(ValidationError, match="listed twice"):
        WorkflowCatalog.model_validate(data)


# ── schedules / 排班 ──


def test_zones_come_from_the_pinned_database_and_keys_cannot_escape():
    assert zone("Asia/Shanghai").utcoffset(datetime(2026, 9, 26)) == timedelta(hours=8)
    for key in ("../etc/passwd", "Asia/../../x", "asia/shanghai", "Mars/Olympus", "", "UTC/../UTC"):
        with pytest.raises(ValueError):
            zone(key)


def test_daily_occurrences_follow_the_local_zone():
    daily = DailySchedule(every="day", at="09:00", timezone="Asia/Shanghai")
    assert next_occurrence(daily, datetime(2026, 9, 26, 0, 0, tzinfo=UTC)) == datetime(2026, 9, 26, 1, 0, tzinfo=UTC)
    assert next_occurrence(daily, datetime(2026, 9, 26, 1, 0, tzinfo=UTC)) == datetime(2026, 9, 27, 1, 0, tzinfo=UTC)
    weekdays = DailySchedule(every="day", at="09:00", timezone="Asia/Shanghai", weekdays=(1,))
    # 2026-09-26 is a Saturday; the next Monday is 09-28. / 2026-09-26 是周六；下一个周一是 09-28。
    assert next_occurrence(weekdays, datetime(2026, 9, 26, 2, tzinfo=UTC)) == datetime(2026, 9, 28, 1, tzinfo=UTC)
    with pytest.raises(ValidationError):
        DailySchedule(every="day", at="25:00", timezone="UTC")
    with pytest.raises(ValidationError):
        DailySchedule(every="day", at="09:00", timezone="UTC", weekdays=(0,))


def test_daylight_saving_gaps_shift_forward_and_overlaps_run_once():
    gap = DailySchedule(every="day", at="02:30", timezone="America/New_York")
    # 2026-03-08 02:30 does not exist in New York; fold=0 maps it to 03:30 EDT = 07:30Z.
    # 2026-03-08 02:30 在纽约不存在；fold=0 映射为 03:30 EDT = 07:30Z。
    assert next_occurrence(gap, datetime(2026, 3, 8, 0, tzinfo=UTC)) == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    overlap = DailySchedule(every="day", at="01:30", timezone="America/New_York")
    first = next_occurrence(overlap, datetime(2026, 11, 1, 0, tzinfo=UTC))
    # 01:30 happens twice on 2026-11-01; only the first (EDT, 05:30Z) runs. / 01:30 出现两次；只运行第一次。
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert next_occurrence(overlap, first) == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)


def test_interval_occurrences_are_anchored_to_utc_multiples():
    every = IntervalSchedule(every="interval", minutes=30)
    assert next_occurrence(every, datetime(2026, 9, 26, 8, 10, tzinfo=UTC)) == datetime(2026, 9, 26, 8, 30, tzinfo=UTC)
    assert next_occurrence(every, datetime(2026, 9, 26, 8, 30, tzinfo=UTC)) == datetime(2026, 9, 26, 9, 0, tzinfo=UTC)


def trigger(**values) -> ScheduleTrigger:
    return ScheduleTrigger(trigger_id="every_30_min", kind="schedule",
                           schedule=IntervalSchedule(every="interval", minutes=30), **values)


def test_only_the_latest_due_occurrence_starts_and_a_backlog_is_missed():
    cursor = datetime(2026, 9, 26, 8, 0, tzinfo=UTC)
    fire, missed, following = due_occurrences(trigger(start_window_s=300), cursor, cursor + timedelta(minutes=2))
    assert fire == [cursor] and missed == [] and following == cursor + timedelta(minutes=30)
    late = cursor + timedelta(minutes=10)
    fire, missed, following = due_occurrences(trigger(start_window_s=300), cursor, late)
    assert fire == [] and missed == [cursor] and following == cursor + timedelta(minutes=30)
    # Six hours down: twelve occurrences passed, only the one inside the window may start.
    # 停机六小时：错过十二个发生时刻，只有启动窗内的那个可以启动。
    now = cursor + timedelta(hours=6, minutes=1)
    fire, missed, following = due_occurrences(trigger(start_window_s=300), cursor, now)
    assert fire == [cursor + timedelta(hours=6)] and len(missed) == 12
    assert following == cursor + timedelta(hours=6, minutes=30)


def test_catch_up_is_explicit_and_bounded():
    cursor = datetime(2026, 9, 26, 8, 0, tzinfo=UTC)
    now = cursor + timedelta(hours=3, minutes=10)
    fire, missed, _ = due_occurrences(trigger(start_window_s=300, catch_up=2, catch_up_window_s=3600), cursor, now)
    assert fire == [cursor + timedelta(hours=2, minutes=30), cursor + timedelta(hours=3)]
    assert len(missed) == 5
    with pytest.raises(ValidationError):
        trigger(start_window_s=600, catch_up=1, catch_up_window_s=300)
