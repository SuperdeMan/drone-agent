"""The local desk preview (D068): the resident desk's catalogs over the S0 world, with truthful labels.

本机任务台预览（D068）：S0 世界上的常驻任务台目录，来源标注如实。
"""

from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


async def test_the_preview_offers_every_workspace_and_labels_its_flights_logical(tmp_path):
    preview = runpy.run_path(str(ROOT / "scripts/desk_preview.py"), run_name="desk_preview")
    world = preview["PreviewWorld"](tmp_path / "case", ROOT, principal="local:tester")
    try:
        await world.settle()
        [campus] = [p for p in (await world.api("local:tester", "projects"))["result"] if p["project_id"] == "campus_s1"]
        assert (campus["scheduling"], campus["business"]) == (True, True)
        assert campus["roles"] == ["admin", "approver", "operator", "reviewer"] and "逻辑飞行" in campus["name"]
        listed = (await world.api("local:tester", "resources.list", project_id="campus_s1"))["result"]
        assert [r["execution_backend"] for s in listed["sites"] for r in s["robots"]] == ["logical_sim"]
        assert (await world.api("local:tester", "workflows.list", project_id="campus_s1"))["ok"]
        assert (await world.api("local:tester", "tasks.list", project_id="campus_s1"))["ok"]
        assert (await world.api("local:tester", "business.summary", project_id="campus_s1"))["ok"]
    finally:
        world.ledger.close()
