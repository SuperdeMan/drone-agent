"""The P5 authorization matrix (D070): zero escapes on the desk platform, and the matrix itself sees an escape.

P5 授权矩阵（D070）：任务台平台上逃逸为 0，且矩阵本身能识别逃逸。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from drone_agent.eval import p5_authz
from drone_agent.fleet.api import METHODS

ROOT = Path(__file__).resolve().parents[2]


async def test_every_method_and_frame_as_every_identity_class_has_no_escape(tmp_path):
    result = await p5_authz.run(tmp_path, ROOT)
    assert result["status"] == "passed" and result["escapes"] == 0, result["escape_rows"]
    assert result["methods"] == len(METHODS) and result["api_rows"] == len(METHODS) * len(p5_authz.IDENTITIES)
    assert result["frame_rows"] >= 8 * 30 and result["audit_denials"] > 0
    rows = json.loads((tmp_path / "authz-rows.json").read_text(encoding="utf-8"))
    stranger = [r for r in rows if r["identity"] == "non_member" and r["expected"] == "forbidden"
                and r["layer"] == "api" and not r["method"].startswith("docks.")]
    assert stranger and all(r["code"] in ("service.not_found", "auth.scope_missing") for r in stranger)


def test_the_matrix_counts_an_answer_a_leak_and_a_named_project_as_escapes():
    matrix = p5_authz.Matrix.__new__(p5_authz.Matrix)
    matrix.roles = {("harness:p5-viewer", "campus_s1"): {"viewer"}, ("harness:p5-harbor", "vendor_s3"): {"viewer"}}
    matrix.world = SimpleNamespace(service=SimpleNamespace(project_of=lambda mission: "campus_s1"))
    answered = matrix.judge_api("viewer", "harness:p5-viewer", "first_party", "approve", "campus_s1", "forbidden",
                                {"ok": True, "result": {"mission": {}}})
    leak = matrix.judge_api("other_project", "harness:p5-harbor", "first_party", "view", "campus_s1", "forbidden",
                            {"ok": False, "issue": {"code": "auth.project_denied"}})
    named = matrix.judge_api("other_project", "harness:p5-harbor", "first_party", "list", "campus_s1", "global",
                             {"ok": True, "result": [{"mission_id": "m-0123456789ab"}]})
    clean = matrix.judge_api("other_project", "harness:p5-harbor", "first_party", "view", "campus_s1", "forbidden",
                             {"ok": False, "issue": {"code": "service.not_found"}})
    assert answered["escape"] == "forbidden_call_answered" and leak["escape"] == "existence_leak"
    assert named["escape"] == "global_answer_names:campus_s1" and clean["escape"] is None
