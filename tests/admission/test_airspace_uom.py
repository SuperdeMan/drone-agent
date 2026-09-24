"""Real-airspace admission from recorded UOM answers fails closed on every gap (WP-M3-19, D016).

基于录制 UOM 应答的真实空域准入对任何缺口都 fail closed（WP-M3-19，D016）。
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from drone_agent.admission.airspace import RecordedUomProvider, SimulatedAirspaceProvider, airspace_issues

ROOT = Path(__file__).resolve().parents[2]
RECORDINGS = ROOT / "eval/airspace/uom_v1"
NOW = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)
WINDOW = (NOW, NOW + timedelta(minutes=30))
REAL = {"airspace_mode": "real"}


def codes(found):
    return [item.code for item in found]


def status(case, now=NOW):
    return RecordedUomProvider(RECORDINGS / case).query("campus_rooftop", now=now)


def test_an_approved_filing_for_this_aircraft_passes_the_real_airspace_check():
    approved = status("approved")
    assert approved.filing_status == "filed" and approved.filing_id == "UOM-2026-000123"
    assert approved.source == "recorded:uom_v1/approved"
    assert airspace_issues("campus_rooftop", REAL, approved, window=WINDOW, registration_id="UAS-SIM-0001") == []


@pytest.mark.parametrize(
    ("case", "registration", "expected"),
    [
        ("expired", "UAS-SIM-0001", ["airspace.not_filed"]),
        ("rejected", "UAS-SIM-0001", ["airspace.not_filed"]),
        ("wrong_volume", "UAS-SIM-0001", ["airspace.filing_scope"]),
        ("remote_id_off", "UAS-SIM-0001", ["airspace.remote_id_inactive"]),
        ("approved", None, ["airspace.registration_mismatch"]),
        ("approved", "UAS-OTHER-9", ["airspace.registration_mismatch"]),
    ],
)
def test_every_gap_in_a_real_filing_fails_closed(case, registration, expected):
    assert codes(airspace_issues("campus_rooftop", REAL, status(case), window=WINDOW,
                                 registration_id=registration)) == expected


def test_a_window_outside_the_approval_and_a_missing_recording_fail_closed():
    late = (datetime(2030, 12, 31, 23, 50, tzinfo=timezone.utc), datetime(2031, 1, 1, 1, 0, tzinfo=timezone.utc))
    at_end = status("approved", now=late[0])
    assert codes(airspace_issues("campus_rooftop", REAL, at_end, window=late,
                                 registration_id="UAS-SIM-0001")) == ["airspace.filing_window"]
    missing = RecordedUomProvider(RECORDINGS / "approved").query("campus_training", now=NOW)
    assert missing.filing_status == "unknown"
    assert codes(airspace_issues("campus_training", REAL, missing, window=WINDOW,
                                 registration_id="UAS-SIM-0001")) == ["airspace.unknown"]


def test_simulation_mode_is_unchanged_and_the_stub_never_claims_a_filing():
    stub = SimulatedAirspaceProvider().query("campus_rooftop", now=NOW)
    assert stub.filing_status == "simulated_unfiled" and stub.filing_id is None
    assert airspace_issues("campus_training", {"airspace_mode": "simulation"}, stub) == []
    assert codes(airspace_issues("campus_rooftop", REAL, stub, window=WINDOW,
                                 registration_id="UAS-SIM-0001")) == ["airspace.not_filed"]
