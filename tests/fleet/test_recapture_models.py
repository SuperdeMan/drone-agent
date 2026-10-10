"""P6 recapture in the workflow kernel (D078): template rules, the selection, predicate facts and the run outcome.

P6 工作流内核中的补拍（D078）：模板规则、选择节点、谓词事实与运行结论。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from drone_agent.fleet.resources import load_catalog
from drone_agent.fleet.workflow import WorkflowEngine
from drone_agent.fleet.workflow_models import WorkflowCatalog, load_workflows, predicate_holds
from drone_agent.mission.registry import Registry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = "configs/workflows/p6_campus_v1.yaml"


def raw() -> dict:
    return yaml.safe_load((ROOT / CATALOG).read_text(encoding="utf-8"))


def template(data: dict, workflow_id: str = "recapture_watch") -> dict:
    return next(w for w in data["workflows"] if w["workflow_id"] == workflow_id)


def node(spec: dict, node_id: str) -> dict:
    return next(n for n in spec["nodes"] if n["node_id"] == node_id)


def test_the_p6_catalog_checks_and_earlier_templates_keep_their_canonical_form():
    catalog = load_workflows(ROOT / CATALOG)
    operations = load_catalog(ROOT / "configs/sites/p1_campus_v1.yaml")
    catalog.check(operations, {s: Registry(ROOT, scene=ROOT / site.scene) for s, site in operations.sites.items()},
                  ROOT)
    spec = catalog.spec("campus_ops", "recapture_watch", 1)
    assert spec.analysis("select").node_id == "analyze" and spec.analysis("analyze").node_id == "analyze"
    for path in sorted((ROOT / "configs/workflows").glob("p[2-5]_*.yaml")):
        dumped = json.dumps(load_workflows(path).model_dump(mode="json"))
        assert "recapture_of" not in dumped and "select_analysis" not in dumped


def mutated(change) -> dict:
    data = raw()
    change(template(data))
    return data


def gate(spec: dict) -> dict:
    return node(spec, "recapture")


@pytest.mark.parametrize("change, message", [
    (lambda s: node(s, "report").update(when=[{"node": "analyze", "output": "recapture", "equals": True}]),
     "only the recapture of analyze reads its recapture"),
    (lambda s: gate(s).update(requires="success"), "whatever it did"),
    (lambda s: gate(s).update(after=[]), "not an ancestor"),
    (lambda s: gate(s).update(when=[]), "whatever it did"),
    (lambda s: gate(s).update(when=[{"node": "analyze", "output": "recapture", "equals": False}]), "whatever it did"),
    (lambda s: gate(s)["params"].update(asset="asset_red"), "flies the robot, volume and asset"),
    (lambda s: gate(s)["params"].update(robot_id="uav_a"), "flies the robot, volume and asset"),
    (lambda s: gate(s)["params"].update(robot_id=None, candidates=["uav_b"]), "fixed robot"),
    (lambda s: node(s, "analyze_recapture")["params"].update(analyzer="vlm_target_s0_v1"), "analyses the recapture"),
    (lambda s: node(s, "analyze")["params"].update(purpose="reinspection"), "not an inspection analysis"),
    (lambda s: node(s, "select").update(requires="success"), "whatever they did"),
    (lambda s: node(s, "select").update(after=["analyze"]), "whatever they did"),
    (lambda s: node(s, "select")["params"].update(analyses=["analyze", "analyze"]), "distinct inspection analyses"),
    (lambda s: node(s, "select")["params"].update(analyses=["analyze", "review"]), "distinct inspection analyses"),
])
def test_recapture_and_selection_rules_are_enforced(change, message):
    with pytest.raises(ValidationError, match=message):
        WorkflowCatalog.model_validate(mutated(change))


def test_a_recapture_is_never_recaptured_and_an_analysis_has_one_recapture():
    def chain(spec):
        spec["nodes"] += [
            {"node_id": "recapture_again", "activity": "submit_mission", "requires": "done",
             "after": ["analyze_recapture"],
             "when": [{"node": "analyze_recapture", "output": "recapture", "equals": True}],
             "params": {"robot_id": "uav_b", "volume_id": "campus_training", "asset": {"input": "asset"},
                        "recapture_of": "analyze_recapture"}},
            {"node_id": "await_again", "activity": "await_mission", "params": {"mission_from": "recapture_again"}}]
        spec["budget"]["max_missions"] = 3

    with pytest.raises(ValidationError, match="never recaptured"):
        WorkflowCatalog.model_validate(mutated(chain))

    def twice(spec):
        second = copy.deepcopy(gate(spec))
        second["node_id"] = "recapture_twice"
        spec["nodes"] += [second, {"node_id": "await_twice", "activity": "await_mission",
                                   "params": {"mission_from": "recapture_twice"}}]
        spec["budget"]["max_missions"] = 3

    with pytest.raises(ValidationError, match="recaptured twice"):
        WorkflowCatalog.model_validate(mutated(twice))


def test_a_reinspection_template_cannot_select():
    def selecting(spec):
        spec["nodes"].append({"node_id": "select", "activity": "select_analysis", "requires": "done",
                              "after": ["analyze"], "params": {"analyses": ["analyze", "analyze"]}})

    data = raw()
    selecting(template(data, "recapture_reinspection"))
    with pytest.raises(ValidationError):
        WorkflowCatalog.model_validate(data)


def row(state: str, reason: str | None = None, detail: dict | None = None, result: dict | None = None) -> dict:
    return {"state": state, "reason": reason, "detail": detail, "result": result}


ANALYSIS = {"analysis_id": "j-1", "verdict": "normal", "suspected": False, "confidence": 1.0,
            "source": "deterministic"}


def nodes(**states) -> dict:
    spec = load_workflows(ROOT / CATALOG).spec("campus_ops", "recapture_watch", 1)
    base = {n.node_id: row("completed") for n in spec.nodes}
    base.update(review=row("skipped", "condition_false"), work_order=row("skipped", "upstream_skipped"))
    base.update(states)
    return base


def spec():
    return load_workflows(ROOT / CATALOG).spec("campus_ops", "recapture_watch", 1)


def test_a_refusal_replaced_by_its_recapture_no_longer_fails_the_run():
    refused = row("failed", "quality.target_exposure", {"job_id": "j-1", "recapture": True})
    done = nodes(analyze=refused, analyze_recapture=row("completed", result=ANALYSIS))
    summary = WorkflowEngine.outcome(done, spec())
    assert summary["result"] == "completed" and summary["recaptured"] == ["analyze"]
    # Without the template the same rows still fail: the replacement is a template fact. / 没有模板时同样的行仍失败。
    assert WorkflowEngine.outcome(done)["result"] == "failed"


def test_a_refusal_that_may_not_be_recaptured_or_a_failed_recapture_still_fails():
    plain = row("failed", "target.mismatch", {"job_id": "j-1", "recapture": False})
    skipped = nodes(analyze=plain, recapture=row("skipped", "condition_false"),
                    await_recapture=row("skipped", "upstream_skipped"),
                    analyze_recapture=row("skipped", "upstream_skipped"),
                    select=row("failed", "analysis.no_usable_capture"),
                    review=row("skipped", "upstream_failed"), work_order=row("skipped", "upstream_failed"))
    assert WorkflowEngine.outcome(skipped, spec())["failed"] == ["analyze", "review", "select", "work_order"]
    twice = nodes(analyze=row("failed", "quality.target_exposure", {"recapture": True}),
                  analyze_recapture=row("failed", "quality.target_exposure", {"recapture": True}),
                  select=row("failed", "analysis.no_usable_capture"),
                  review=row("skipped", "upstream_failed"), work_order=row("skipped", "upstream_failed"))
    summary = WorkflowEngine.outcome(twice, spec())
    assert summary["result"] == "failed" and "analyze" not in summary["failed"]
    assert "analyze_recapture" in summary["failed"]


def test_conditions_read_a_failed_analysis_only_through_its_recorded_decision():
    facts = WorkflowEngine._facts(nodes(analyze=row("failed", "quality.blurry", {"recapture": True}),
                                        recapture=row("pending")))
    assert facts["analyze"] == {"recapture": True}
    gate_predicate = spec().node("recapture").when[0]
    assert predicate_holds(gate_predicate, facts)
    for detail in (None, {}, {"recapture": "yes"}, {"recapture": False}):
        facts = WorkflowEngine._facts(nodes(analyze=row("failed", "quality.blurry", detail)))
        assert not predicate_holds(gate_predicate, facts)
    completed = WorkflowEngine._facts(nodes(analyze=row("completed", result=ANALYSIS)))
    assert not predicate_holds(gate_predicate, completed)
