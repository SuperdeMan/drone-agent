"""The P4 S2 retrieval report (D065 §7): relevance from the manifest, AP@10 and device identification.

P4 S2 检索报告（D065 §7）：取自清单的相关性、AP@10 与设备识别。
"""

from __future__ import annotations

import numpy as np

from drone_agent.eval.p4_retrieval import average_precision, relevant, report


def test_average_precision_at_10_normalises_by_the_reachable_relevant_items():
    assert average_precision([True, False, True], 2) == round((1 / 1 + 2 / 3) / 2, 4)
    assert average_precision([False] * 10 + [True], 1) == 0.0
    assert average_precision([True] * 12, 12) == 1.0
    assert average_precision([False, False], 0) is None


def test_relevance_reads_defects_boards_and_truth_from_the_manifest():
    sample = {"asset": "board_2", "truth": "anomalous", "defects": "melt,scratch"}
    assert relevant(sample, {"defect": "scratch"}) and not relevant(sample, {"defect": "bent"})
    assert relevant(sample, {"asset": "board_2"}) and not relevant(sample, {"asset": "board_1"})
    assert relevant(sample, {"truth": "anomalous"})


def test_report_ranks_by_cosine_and_identifies_boards_from_their_references():
    samples = [{"sample_id": f"r{b}", "split": "reference", "asset": f"board_{b}", "category": "normal",
                "truth": "normal", "defects": "normal"} for b in (1, 2)]
    samples += [{"sample_id": f"t{i}", "split": "test", "asset": f"board_{1 + i % 2}", "category": "anomalous"
                 if i < 2 else "normal", "truth": "anomalous" if i < 2 else "normal",
                 "defects": "melt" if i < 2 else "normal"} for i in range(4)]
    manifest = {"assets": {"board_1": {}, "board_2": {}}, "samples": samples}
    # Board identity on the first axis, damage on the second. / 第一轴为板型，第二轴为损伤。
    vectors = {"r1": [1, 0, 0], "r2": [0, 1, 0], "t0": [1, 0, 1], "t1": [0, 1, 1], "t2": [1, 0, 0], "t3": [0, 1, 0]}
    config = {"classes": {"defect_melt": {"match": {"defect": "melt"}}, "board_1": {"match": {"asset": "board_1"}}}}
    queries = {"classes": {"defect_melt": {"embeddings": [[0, 0, 1]]}, "board_1": {"embeddings": [[1, 0, 0]]}}}
    result = report(manifest, config, queries, lambda s: np.asarray(vectors[s["sample_id"]], dtype=np.float32))
    assert result["corpus"] == 4 and result["threshold"] is None
    assert result["text_to_image"]["per_query"]["defect_melt"]["ap_at_10"] == 1.0
    assert result["text_to_image"]["per_query"]["board_1"]["top10"][:2] == ["t2", "t0"]
    assert result["image_to_reference"]["top1"] == 1.0
