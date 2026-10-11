"""D082: the cross-site reference check on the labelled corpora and the desk cases; deterministic, no model.

D082：跨站点指代检查在带标注语料与任务台用例上的结果；确定性，不调用模型。
"""

from __future__ import annotations

from pathlib import Path

from drone_agent.eval.reference_check import evaluate

ROOT = Path(__file__).resolve().parents[2]


def test_no_request_that_must_be_planned_is_rejected_and_every_desk_case_behaves():
    result = evaluate(ROOT)
    assert result["false_rejections"] == 0 and result["status"] == "passed", result["corpora"]
    assert result["desk"]["as_expected"] == result["desk"]["cases"] == 15
    # Coverage as of D082: references only by description, another volume or a foreign asset id are left to the
    # planner and the approver. / D082 时的覆盖：只靠描述、其他体积或外站资产 id 的指代留给规划器与审批人。
    assert (result["caught"], result["missed"]) == (17, 5)
    missed = {row["id"] for report in result["corpora"].values() for row in report["missed"]}
    assert missed == {"cross-foreign-id-en", "cross-volume-en", "cross-campus-blue-en", "cross-rooftop-en",
                      "cross-rooftop-deck-en"}
