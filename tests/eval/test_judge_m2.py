"""M2 judge on real onboard journals from the in-process loop plus synthetic truth (WP-M2-19).

M2 裁判：使用进程内闭环产生的真实机载账本与合成真值（WP-M2-19）。
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from drone_agent.eval.judge_m2 import judge_case
from drone_agent.eval.m2_prepare import load_suite, materialize
from drone_agent.fleet.provision import provision
from drone_agent.runtime.signing import SigningKey
from tests.fleet.harness import ROOT, build_loop, request


async def build_case(tmp_path, *, frames=(), expected=None):
    loop = build_loop(tmp_path)
    view = await loop.service.submit(request())
    mission_id = view["mission"]["mission_id"]
    loop.service.approve(mission_id, 1, approver="harness:test", package_hash=view["versions"][0]["package_hash"])
    await loop.sync()
    truth: list = []
    await loop.fly(loop.inbox_package(), epoch=1, frames=frames, truth=truth)
    missions = await loop.sync()
    if missions[mission_id]["current_version"] == 2:
        await loop.fly(loop.inbox_package(), epoch=2, truth=truth)
        await loop.sync()
    (tmp_path / "input").mkdir()
    (tmp_path / "input/scenario.json").write_text(json.dumps({
        "scenario": "nl_inspect_red", "seed": 7, "source_sha": "test",
        "expected": expected or {"status": "completed", "versions": 1, "replans": 0, "classification": "completed"}}))
    (tmp_path / "truth").mkdir()
    (tmp_path / "truth/truth.jsonl").write_text("\n".join(json.dumps({k: v for k, v in row.items() if k != "mono"})
                                                          for row in truth) + "\n")
    (tmp_path / "service/ready.json").write_text(json.dumps({"signer_key_id": loop.key.key_id}))
    (tmp_path / "service-export").mkdir()
    (tmp_path / "service-export/view.json").write_text(json.dumps(loop.service.view(mission_id)))
    return loop, mission_id


async def test_nominal_case_passes_online_and_from_the_recordings(tmp_path):
    await build_case(tmp_path)
    online, replayed = judge_case(tmp_path, ROOT), judge_case(tmp_path, ROOT, use_replay=True)
    assert online["passed"], online["problems"]
    assert (online["classification"], online["false_success_reports"], online["truly_inspected"]) == (
        "completed", 0, ["asset_red"])
    assert all(online[k] == replayed[k] for k in ("classification", "false_success_reports", "problems"))


async def test_replanned_case_needs_both_versions_and_one_replan(tmp_path):
    await build_case(tmp_path, frames=[None, None, None],
                     expected={"status": "completed", "versions": 2, "replans": 1, "classification": "completed"})
    result = judge_case(tmp_path, ROOT)
    assert result["passed"], result["problems"]
    assert result["flown_versions"] == [1, 2] and result["report_targets"] == {"asset_red": "completed"}


async def test_a_report_the_truth_does_not_support_is_a_false_success(tmp_path):
    await build_case(tmp_path)
    rows = [json.loads(line) for line in (tmp_path / "truth/truth.jsonl").read_text().splitlines()]
    for row in rows:
        row["position"][0] += 3.0  # the aircraft was never above the marker / 飞行器从未到达标记上方
    (tmp_path / "truth/truth.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    result = judge_case(tmp_path, ROOT)
    assert not result["passed"] and result["false_success_reports"] >= 2
    assert "report_completed_without_true_inspection:asset_red" in result["problems"]


async def test_an_incomplete_service_mirror_and_a_wrong_signer_fail_the_case(tmp_path):
    loop, _ = await build_case(tmp_path)
    view = json.loads((tmp_path / "service-export/view.json").read_text())
    view["versions"][0]["journals"]["guardian"]["rows"] -= 1
    (tmp_path / "service-export/view.json").write_text(json.dumps(view))
    (tmp_path / "service/ready.json").write_text(json.dumps({"signer_key_id": SigningKey.generate().key_id}))
    problems = judge_case(tmp_path, ROOT)["problems"]
    assert "v1:service_mirror_incomplete:guardian" in problems
    assert "v1:package_not_verified_with_service_key" in problems


def test_every_suite_case_materializes_with_its_own_scripted_answer(tmp_path):
    suite = load_suite(ROOT)
    assert {s["id"] for s in suite["scenarios"]} >= {"nl_inspect_red", "replan_degraded_image", "service_outage",
                                                    "nl_refused", "nl_adversarial_scope"}
    for scenario in suite["scenarios"]:
        for seed in suite["seeds"]:
            out = tmp_path / f"{scenario['id']}-{seed}"
            metadata = materialize(ROOT, out, scenario["id"], seed, "test")
            fixture = json.loads((out / "planner-fixtures.json").read_text(encoding="utf-8"))
            assert fixture["source"] == "scripted" and list(fixture["answers"]) == [metadata["text"]]
    assert json.loads((tmp_path / "replan_degraded_image-7/fault-v1.json").read_text())["kind"] == "image_degraded"
    texts = {materialize(ROOT, tmp_path / f"red-{s}", "nl_inspect_red", s, "t")["text"] for s in suite["seeds"]}
    assert len(texts) == 3  # several phrasings / 多种措辞


def test_provisioning_keeps_the_signing_key_and_reports_only_public_facts(tmp_path):
    first = provision(tmp_path / "secrets")
    second = provision(tmp_path / "secrets")
    assert first["signer_key_id"] == second["signer_key_id"] and second["created"] == []
    assert set(first["fingerprints"]) == {"ca", "service", "robot"}
    for name in ("approval-signing.key", "ca/ca.key", "service-tls/service.key", "robot-tls/robot.key"):
        assert (tmp_path / "secrets" / name).is_file()
    assert "PRIVATE" not in json.dumps(first)
    if sys.platform != "win32":
        mode = stat.S_IMODE(os.stat(tmp_path / "secrets/approval-signing.key").st_mode)
        assert mode == 0o600


@pytest.mark.parametrize("scenario", ["nl_refused", "nl_adversarial_scope"])
def test_no_flight_scenarios_expect_nothing_delivered(scenario):
    expected = next(s for s in load_suite(ROOT)["scenarios"] if s["id"] == scenario)["expected"]
    assert expected["flight"] is False and expected["classification"] == "not_completed"


async def test_viewer_renders_each_version_with_the_planning_band_and_tables(tmp_path):
    from drone_agent.eval.viewer import build_page, load_m2_case

    case = tmp_path / "replan_degraded_image-7"
    case.mkdir()
    await build_case(case, frames=[None, None, None],
                     expected={"status": "completed", "versions": 2, "replans": 1, "classification": "completed"})
    records = load_m2_case(case, ROOT)
    assert [r["id"] for r in records] == ["replan_degraded_image-7 · v1", "replan_degraded_image-7 · v2"]
    for record in records:
        assert any(e["lane"] == "planning" for e in record["events"])
        assert {"planning", "approval", "report"} <= {t["id"] for t in record["tables"]}
        assert record["gallery"] and record["integrity"]["journals"] == {"executive": "ok", "guardian": "ok"}
    approvals = next(t for t in records[0]["tables"] if t["id"] == "approval")["rows"]
    assert [row[1] for row in approvals] == ["harness:test", "policy:m2_approval@v1"]
    page = build_page([case], root=ROOT, output=tmp_path / "viewer.html")
    assert "planning" in page.read_text(encoding="utf-8")
