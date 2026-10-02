"""The local desk preview (D068): the resident desk's catalogs over the S0 world, with truthful labels.

本机任务台预览（D068）：S0 世界上的常驻任务台目录，来源标注如实。
"""

from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


async def test_the_preview_offers_every_workspace_and_labels_its_flights_truthfully(tmp_path):
    preview = runpy.run_path(str(ROOT / "scripts/desk_preview.py"), run_name="desk_preview")
    world = preview["PreviewWorld"](tmp_path / "case", ROOT, principal="local:tester")
    try:
        await world.settle()
        projects = {p["project_id"]: p for p in (await world.api("local:tester", "projects"))["result"]}
        campus = projects["campus_s1"]
        assert (campus["scheduling"], campus["business"]) == (True, True)
        assert campus["roles"] == ["admin", "approver", "operator", "reviewer"] and "逻辑飞行" in campus["name"]
        backends = {}
        for project in ("campus_s1", "fleet_s0", "vendor_s3"):
            listed = (await world.api("local:tester", "resources.list", project_id=project))["result"]
            backends[project] = [r["execution_backend"] for s in listed["sites"] for r in s["robots"]]
            assert (await world.api("local:tester", "audit.list", project_id=project, before=None, limit=5))["ok"]
        assert backends == {"campus_s1": ["logical_sim"], "fleet_s0": ["logical_sim", "logical_sim"],
                            "vendor_s3": ["vendor_protocol_sim"]}
        assert (await world.api("local:tester", "workflows.list", project_id="campus_s1"))["ok"]
        assert (await world.api("local:tester", "tasks.list", project_id="fleet_s0"))["ok"]
        assert (await world.api("local:tester", "business.summary", project_id="campus_s1"))["ok"]
    finally:
        world.service.business.jobs.stop()
        world.ledger.close()
