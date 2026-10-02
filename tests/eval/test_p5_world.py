"""P5 S0 / S3 world (D070–D072): generated maps, one road case, one vendor case and the project audit trail.

The full matrices (P5-R01–R08, P5-V01–V12 × 3 seeds) run on the spot in the release gate.

P5 S0 / S3 世界（D070–D072）：生成的地图、一个道路用例、一个厂商用例与项目审计记录。完整矩阵（P5-R01–R08、P5-V01–V12 ×
3 种子）由发布门禁当场运行。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drone_agent.eval import p5_world
from drone_agent.eval.p5_layout import SITES, render

ROOT = Path(__file__).resolve().parents[2]


def scenario(case_id: str) -> tuple[dict, str]:
    suite = p5_world.suite(ROOT)
    for layer in ("s0", "s3"):
        for item in suite[layer]:
            if item["id"] == case_id:
                return item, layer.upper()
    raise KeyError(case_id)


def test_p5_site_maps_are_regenerated_byte_for_byte():
    for path, registry, robot, suffix in SITES:
        assert (ROOT / path).read_bytes() == render(ROOT, registry, robot, suffix).encode("utf-8"), path


@pytest.mark.parametrize("case_id", ["p5_r01_clear", "p5_v01_nominal"])
async def test_one_road_case_and_one_vendor_case_pass_their_independent_judge(tmp_path, case_id):
    item, layer = scenario(case_id)
    result = await p5_world.run_case(tmp_path / case_id, ROOT, item, 7, "test", layer)
    assert result["passed"], json.dumps(result, indent=1, default=str)
    assert not any(result["counts"].values())


async def test_a_vendor_mission_records_its_own_backend_and_no_platform_safety(tmp_path):
    item, layer = scenario("p5_v01_nominal")
    case = tmp_path / "v01"
    await p5_world.run_case(case, ROOT, item, 19, "test", layer)
    views = json.loads((case / "service-export/views.json").read_text(encoding="utf-8"))
    vendor = [v for v in views.values() if (v.get("binding") or {}).get("execution_backend") == "vendor_protocol_sim"]
    assert vendor and vendor[0]["versions"][0]["provenance"]["execution_backend"] == "vendor_protocol_sim"
    assert vendor[0]["vendor"]["safety"] == "vendor_responsibility"
    assert vendor[0]["vendor"]["profile"]["verified_against_firmware"] is False
    assert {e["kind"] for e in vendor[0]["versions"][0]["events"] if e["journal"] == "vendor"} >= {
        "task", "command", "reply", "event", "terminal", "grounded"}


async def test_the_audit_trail_shows_the_chain_to_members_only_and_records_refused_members(tmp_path):
    world = p5_world.P5World(tmp_path / "audit", ROOT, seed=7)
    try:
        await world.settle()
        await p5_world.r04_dismissed(world)
        denied = await world.api(p5_world.VIEWER, "workflows.start", project_id="campus_ops", workflow_id="road_watch",
                                 request_id="viewer-start", inputs={"segment": "road_north"})
        assert denied["issue"]["code"] == "auth.project_denied"
        page = await world.api(p5_world.VIEWER, "audit.list", project_id="campus_ops", before=None, limit=200)
        assert page["ok"], page
        actions = [e["action"] for e in page["result"]["entries"]]
        for wanted in ("mission.submitted", "mission.approved", "finding.reviewed", "access.denied"):
            assert wanted in actions, actions
        assert all(e["object"] is None or e["object"]["kind"] != "dock" or e["object"]["id"] == "dock_fa"
                   for e in page["result"]["entries"])
        assert "status.accepted" not in actions and "lease.taken" not in actions
        refused = next(e for e in page["result"]["entries"] if e["action"] == "access.denied")
        assert refused["actor"] == p5_world.VIEWER and refused["detail"]["method"] == "workflows.start"
        small = await world.api(p5_world.VIEWER, "audit.list", project_id="campus_ops", before=None, limit=3)
        older = await world.api(p5_world.VIEWER, "audit.list", project_id="campus_ops",
                                before=small["result"]["next"], limit=3)
        assert len(small["result"]["entries"]) == 3 and older["ok"]
        first = {(e["at"], e["action"]) for e in small["result"]["entries"]}
        assert not first & {(e["at"], e["action"]) for e in older["result"]["entries"]}
        for actor, project in ((p5_world.HARBOR, "campus_ops"), ("tailnet:stranger@x", "campus_ops"),
                               (p5_world.VIEWER, "harbor_ops")):
            hidden = await world.api(actor, "audit.list", project_id=project, before=None, limit=10)
            assert hidden["issue"]["code"] == "service.not_found"
        agent = await world.api("a2a:client", "audit.list", trust="third_party", project_id="campus_ops", before=None,
                                limit=10)
        assert not agent["ok"]
        harbor = await world.api(p5_world.HARBOR, "audit.list", project_id="harbor_ops", before=None, limit=50)
        assert harbor["ok"] and not any(e["action"] == "access.denied" for e in harbor["result"]["entries"])
    finally:
        world.service.business.jobs.stop()
        world.ledger.close()
