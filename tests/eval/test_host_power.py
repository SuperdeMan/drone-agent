"""The host power log rule of the D085 gate: mains power through a whole measurement, without long gaps.

D085 门禁的主机电源记录规则：测量全程接交流电，且没有长间隔。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from drone_agent.eval.host_power import covered, sample

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def log(states: list[str], every_s: float = 5.0) -> list[dict]:
    return [{"at": (T0 + timedelta(seconds=i * every_s)).isoformat(), "ac": state} for i, state in enumerate(states)]


def test_a_measurement_counts_only_when_every_sample_inside_it_is_on_mains():
    start, end = T0.isoformat(), (T0 + timedelta(seconds=60)).isoformat()
    assert covered(log(["online"] * 13), start, end)["ok"]
    battery = covered(log(["online"] * 5 + ["battery"] + ["online"] * 7), start, end)
    assert not battery["ok"] and battery["not_online"]
    assert not covered(log(["online"] * 5 + ["unknown"] + ["online"] * 7), start, end)["ok"]


def test_a_gap_in_the_log_or_no_sample_at_all_is_not_coverage():
    start, end = T0.isoformat(), (T0 + timedelta(seconds=120)).isoformat()
    sparse = covered(log(["online"] * 3, every_s=60), start, end)
    assert sparse["ok"] is False and sparse["largest_gap_s"] == 60
    assert covered([], start, end)["ok"] is False


def test_a_sample_names_the_line_status():
    assert sample()["ac"] in ("online", "battery", "unknown")
