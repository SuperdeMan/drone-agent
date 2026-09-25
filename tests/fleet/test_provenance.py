"""Sources survive restarts, cannot authorize execution, and never promote synthetic output.

来源在重启后保持、不产生执行权限、不把合成输出提升为实调结果。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from drone_agent.fleet.api import dispatch
from drone_agent.fleet.provenance import (
    NAMESPACE,
    EvidenceOrigin,
    ModelUse,
    RunProvenance,
    digest,
    export,
    project,
    provider_source,
    source_context,
)
from drone_agent.fleet.service import MissionService
from drone_agent.providers import GuardedProvider, Recording, ReplayProvider, ScriptedProvider
from tests.fleet.harness import ROOT, SCENE, build_loop, request


async def test_model_name_cannot_promote_scripted_planning_and_api_cannot_select_real_backend(tmp_path):
    loop = build_loop(tmp_path)
    loop.service.planner.provider.model = "MiniMax-M3"
    view = await loop.service.submit(request())
    saved = view["versions"][0]["provenance"]
    assert saved["execution_backend"] == "logical_sim"
    assert saved["planning"]["source"] == "scripted"
    assert saved["planning"]["reported_model_id"].endswith("MiniMax-M3")
    assert saved["analysis_sources"] == ["not_run"]
    assert digest({k: v for k, v in saved.items() if k != "sha256"}) == saved["sha256"]
    reply = await dispatch(loop.service, {"method": "submit", "actor": "tailnet:test", "trust": "first_party",
                                          "params": {"text": "x", "volume_id": "campus_training", "asset_ids": [],
                                                     "idempotency_key": "malicious", "execution_backend": "real_device"}})
    assert not reply["ok"] and len(loop.ledger.missions()) == 1
    with pytest.raises(ValidationError):
        source_context(ROOT, SCENE, loop.service.registry.sha256, backend="real_device")


async def test_source_binding_survives_restart_and_decline_without_schema_changes(tmp_path):
    loop = build_loop(tmp_path)
    schema = loop.ledger._rows("SELECT name, sql FROM sqlite_master ORDER BY name")
    view = await loop.service.submit(request())
    mission_id = view["mission"]["mission_id"]
    original = view["versions"][0]["provenance"]
    restarted = MissionService(root=ROOT, scene=SCENE, ledger=loop.ledger, hub=loop.hub, signing_key=loop.key,
                               approval_policy=loop.service.policy, planner=loop.service.planner)
    assert restarted.source.run_id != original["run"]["run_id"]
    assert restarted.view(mission_id)["versions"][0]["provenance"] == original
    declined = restarted.decline(mission_id, 1, approver="tailnet:test")
    assert declined["versions"][0]["provenance"] == original
    assert loop.ledger._rows("SELECT name, sql FROM sqlite_master ORDER BY name") == schema
    record = loop.ledger.version(mission_id, 1)
    changed = dict(record["decision"])
    changed[NAMESPACE] = {}
    with pytest.raises(ValueError, match="immutable"):
        loop.ledger.update_version(mission_id, 1, decision=changed)


def test_legacy_records_remain_unknown_and_tampered_seals_are_rejected(tmp_path):
    loop = build_loop(tmp_path)
    loop.ledger.record_request(request(), "legacy-mission")
    loop.ledger.record_version("legacy-mission", 1, status="refused", origin="console")
    before = loop.ledger.version("legacy-mission", 1)
    p = project(before)
    assert p.status == p.execution_backend == p.planning.source == "legacy_unknown"
    assert p.run is None and p.analysis_sources == ("legacy_unknown",)
    assert loop.service.provenance("legacy-mission")[0] == export(before)
    assert loop.ledger.version("legacy-mission", 1) == before
    header = loop.service.source.header("legacy-mission", 1, ModelUse())
    loop.ledger.record_source("legacy-mission", 1, "run", header)
    record = loop.ledger.version("legacy-mission", 1)
    record["decision"][NAMESPACE]["run"]["body"]["execution_backend"] = "real_device"
    with pytest.raises(ValueError, match="digest"):
        project(record)


def test_replay_origin_comes_from_fixture_kind_and_projection_preserves_mixed_media():
    for kind, expected in (("recorded", "recorded_model"), ("scripted", "scripted")):
        fixture = Recording(source=kind, provider_id="minimax", model="MiniMax-M3")
        source, recording = provider_source(ReplayProvider(fixture))
        assert source == expected and recording == digest(fixture.model_dump(mode="json"))
    assert provider_source(object())[0] == "unknown"
    images = tuple(EvidenceOrigin(evidence_id=f"image-{i}", media_sha256=str(i) * 64, source=source, run_id="run")
                   for i, source in enumerate(("sim_render", "recorded_real"), 1))
    projection = RunProvenance(mission_id="mixed", mission_version=1, status="legacy_unknown", imagery=images)
    assert {i["source"] for i in projection.model_dump(mode="json")["imagery"]} == {"sim_render", "recorded_real"}
    with pytest.raises(ValidationError):
        projection.execution_backend = "real_device"


async def test_cache_does_not_turn_a_scripted_provider_into_a_recorded_model():
    provider = GuardedProvider(ScriptedProvider([{"content": "answer"}], model="claimed-live"), provider_id="test")
    args = ([{"role": "user", "content": "source test"}], "claimed-live", 0.1, 20)
    await provider.complete(*args)
    assert provider_source(provider)[0] == "scripted"
    await provider.complete(*args)
    source, cache_digest = provider_source(provider)
    assert provider.last_source == "cache" and source == "scripted" and len(cache_digest) == 64


async def test_evidence_analysis_and_report_sources_are_independent_and_do_not_change_verdicts(tmp_path):
    loop = build_loop(tmp_path)
    view = await loop.service.submit(request())
    mission_id = view["mission"]["mission_id"]
    loop.service.approve(mission_id, 1, approver="tailnet:test", package_hash=view["versions"][0]["package_hash"])
    await loop.sync()
    await loop.fly(loop.inbox_package(), epoch=1)
    await loop.sync()
    before = loop.service.view(mission_id)
    assert before["evidence"] and all(e["provenance"]["source"] == "test_fixture" for e in before["evidence"])
    loop.service.vision = (ScriptedProvider([{"content": json.dumps({"anomaly_suspected": True,
                                                                    "confidence": 0.99})}], model="vision-name"),
                           "vision-name")
    assert await loop.service.enrich(mission_id) == 1
    after = loop.service.view(mission_id)
    p = after["versions"][0]["provenance"]
    assert p["planning"]["source"] == "scripted" and p["analysis_sources"] == ["scripted"]
    assert p["analysis"][0]["use"]["input_sha256"] == after["evidence"][0]["sha256"]
    assert after["report"]["targets"] == before["report"]["targets"]
    assert after["report"]["rows"] == before["report"]["rows"]
    assert after["report"]["provenance"] == [p]
    # Duplicate sync cannot replace a recorded acquisition or add another source. / 重复同步不能替换采集记录或新增来源。
    await loop.sync()
    assert loop.service.view(mission_id)["versions"][0]["provenance"] == p
