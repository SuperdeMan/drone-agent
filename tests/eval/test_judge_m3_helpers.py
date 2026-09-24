"""The M3 judge's evidence helpers: simulator stalls, resource receipts and event-detection summaries (D040, D044, D045).

M3 裁判的证据辅助函数：仿真停顿、资源回执与事件检测汇总（D040、D044、D045）。
"""

from datetime import datetime, timedelta, timezone

from drone_agent.eval.judge_m3 import edge_summary, resource_summary, simulator_stalls, stall_voids

START = datetime(2026, 9, 24, 9, 53, 35, tzinfo=timezone.utc)


def truth_row(wall_s, sim_s, position=(0.0, 0.0, 4.0)):
    return {"timestamp": (START + timedelta(seconds=wall_s)).isoformat(), "sim_time": sim_s, "position": list(position)}


def test_a_frozen_simulator_is_a_stall_but_a_late_collector_is_not():
    truth = [truth_row(0.0, 57.0), truth_row(0.06, 57.06),
             truth_row(0.614, 57.116),   # frozen: 0.554 s wall, 0.056 s simulated / 冻结
             truth_row(0.674, 57.176),
             truth_row(1.274, 57.776)]  # collector late, simulation kept pace / 采集滞后，仿真照常
    stalls = simulator_stalls(truth)
    assert [(s["wall_s"], s["sim_s"]) for s in stalls] == [(0.554, 0.056)]


def test_resource_receipts_report_cpu_throttling_and_memory():
    rows = [
        {"wall": 0.0, "service": "sitl", "usage_usec": 0, "nr_periods": 0, "nr_throttled": 0, "memory_bytes": 600 << 20},
        {"wall": 1.0, "service": "sitl", "usage_usec": 1_500_000, "nr_periods": 10, "nr_throttled": 2,
         "memory_bytes": 650 << 20},
        {"wall": 2.0, "service": "sitl", "usage_usec": 2_500_000, "nr_periods": 20, "nr_throttled": 3,
         "memory_bytes": 640 << 20},
        {"wall": 1.0, "service": "_host", "load1": 5.5, "cpu_some_avg10": 30.2},
        {"wall": 2.0, "service": "_host", "load1": 6.1, "cpu_some_avg10": 24.0},
        {"wall": 1.0, "service": "edge", "usage_usec": 5, "nr_periods": 0, "nr_throttled": 0, "memory_bytes": 1},
    ]
    summary = resource_summary(rows)
    assert summary["sitl"] == {"cpu_mean_cores": 1.25, "cpu_max_1s_cores": 1.5, "throttled_fraction": 0.15,
                               "memory_max_mib": 650.0}
    assert summary["_host"] == {"load1_max": 6.1, "cpu_some_avg10_max": 30.2}
    assert "edge" not in summary  # one sample is no rate / 单个采样算不出速率


def test_edge_summary_scores_only_unambiguous_frames_against_truth():
    assets = {"asset_green": {"position": [0, 28, 0], "visual_signature": "green", "size_m": 2},
              "asset_red": {"position": [4, 4, 0], "visual_signature": "red", "size_m": 2}}
    truth = [truth_row(0.0, 0.0, (0.0, 28.0, 4.0)), truth_row(1.0, 1.0, (0.0, 15.0, 4.0)),
             truth_row(2.0, 2.0, (1.0, 4.0, 4.0))]

    def row(wall_s, label, latency=210.0):
        return {"wall_time": 0, "captured": (START + timedelta(seconds=wall_s)).isoformat(), "inference_ms": 200.0,
                "latency_ms": latency, "label": label, "probability": 0.9, "replan_trigger": False, "sent": True,
                "dropped_frames": 4}

    rows = [row(0.0, "marker_green"), row(1.0, "marker_red", 950.0), row(2.0, "marker_red")]
    summary = edge_summary(rows, truth, assets)
    # Above the green marker: scored and right; far from every marker: scored and wrong; 3 m from the red marker
    # centre at 4 m height, between the inscribed (2.5 m) and circumscribed footprint circles: not scored.
    # 绿色标记正上方：计分且正确；远离所有标记：计分且错误；4 米高度下距红色标记中心 3 米，介于足迹内切圆（2.5 米）与外接圆
    # 之间：不计分。
    assert summary["truth_scored"] == 2 and summary["truth_agreement"] == 0.5
    assert summary["latency_ms"] == {"p50": 210.0, "p99": 950.0, "max": 950.0}
    assert summary["labels"] == {"marker_green": 1, "marker_red": 2} and summary["dropped_frames"] == 12
    assert edge_summary([], truth, assets) == {"inferences": 0}


def intervention(wall_s, reason):
    return {"timestamp": (START + timedelta(seconds=wall_s)).isoformat(), "data": {"reason": reason}}


def test_only_a_freshness_recovery_during_or_right_after_a_stall_is_void():
    stall = [{"start": START.timestamp() + 10.0, "end": START.timestamp() + 10.6}]
    common = {"injected_at": None, "expected_reason": None, "problems": [], "false_success": 0}
    # During the freeze, and 1 s after it: void. / 冻结期间及恢复后 1 秒：作废。
    assert stall_voids([intervention(10.5, "observation_stale")], stall, **common)
    assert stall_voids([intervention(11.6, "autonomy_unavailable")], stall, **common)
    # Too late, not a freshness recovery, or a safety finding: never void. / 太晚、非新鲜度恢复或安全发现：绝不作废。
    assert not stall_voids([intervention(12.5, "observation_stale")], stall, **common)
    assert not stall_voids([intervention(10.5, "energy_low")], stall, **common)
    assert not stall_voids([intervention(10.5, "observation_stale")], stall,
                           **{**common, "problems": ["truth_clearance_violation:wall_a"]})
    assert not stall_voids([intervention(10.5, "observation_stale")], stall, **{**common, "false_success": 1})


def test_after_an_injection_the_scenarios_own_recovery_is_never_voided():
    stall = [{"start": START.timestamp() + 10.0, "end": START.timestamp() + 10.6}]
    injected = START.timestamp() + 5.0
    kwargs = {"injected_at": injected, "problems": ["expected_follow_up_not_observed"], "false_success": 0}
    assert not stall_voids([intervention(10.5, "observation_stale")], stall, expected_reason="observation_stale",
                           **kwargs)
    # The event detector's kill expects no recovery at all: a stall-made abort afterwards is void.
    # 杀掉事件检测不期望任何恢复：之后由停顿造成的中止作废。
    assert stall_voids([intervention(10.5, "observation_stale")], stall, expected_reason=None, **kwargs)
