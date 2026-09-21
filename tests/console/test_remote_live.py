"""Verify cloud job identity, mailbox serialization and live freshness boundaries.

验证云端任务身份、信箱串行化与实时新鲜度边界。
"""

import copy
import json
import runpy
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIVE = runpy.run_path(str(ROOT / "scripts/remote_live.py"))
RUN = "20260921T120000Z-0123abcd"
DEPLOY = "20260921T110000Z-0123abcd"


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    deployment = tmp_path / "releases" / DEPLOY
    deployment.mkdir(parents=True)
    (deployment / "manifest.json").write_text(json.dumps({"source_sha": "a" * 40, "control_sha256": "b" * 64}))
    # Pure unit tests observe lock ownership; Linux cloud tests exercise real flock. / 纯单测观察锁所有权，Linux 云端测试执行实际 flock。
    if sys.platform == "win32":
        monkeypatch.setitem(sys.modules, "fcntl", types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *_: None))
    start = LIVE["start"]
    spawned = []

    def spawn(argv, **kwargs):
        spawned.append((argv, kwargs))
        return types.SimpleNamespace(pid=1234)

    monkeypatch.setattr(start.__globals__["subprocess"], "Popen", spawn)
    monkeypatch.setitem(start.__globals__, "process_identity", lambda _pid: "process-identity")
    start(tmp_path, deployment, {"action": "live_start", "run_id": RUN, "seed": 7})
    return tmp_path, deployment, spawned


def seed_runtime(root):
    case = root / "artifacts" / DEPLOY / ("m1-" + RUN) / "interactive-7"
    (case / "aircraft").mkdir(parents=True)
    now = datetime.now(timezone.utc)
    observation = {"timestamp": now.isoformat(), "valid_until": (now + timedelta(seconds=0.5)).isoformat(), "flight_mode": "MISSION"}
    state = {"monotonic": time.monotonic(), "observation": observation, "active_step": "fly_route", "safety": "proceed", "reason": "", "command_pending": False}
    (case / "aircraft/status.json").write_text(json.dumps(state))
    executive = {"kind": "skill_state", "data": {"step_id": "fly_route", "mission_id": "m-one", "state": "running"}, "timestamp": now.isoformat()}
    guardian = {"kind": "lease", "data": {"mission_id": "m-one", "mission_version": 1, "lease_epoch": 1}, "timestamp": now.isoformat()}
    for name, row in (("executive", executive), ("guardian", guardian)):
        (case / f"aircraft/{name}.jsonl").write_text(json.dumps(row) + "\n")
    return case


def operation(**changes):
    return {"action": "live_operate", "run_id": RUN, "request_id": "operator-123", "operation": "pause", "mission_id": "m-one", "step_id": "fly_route", **changes}


def test_start_is_detached_pinned_and_idempotent(cloud):
    root, deployment, spawned = cloud
    request = {"action": "live_start", "run_id": RUN, "seed": 7}
    assert LIVE["start"](root, deployment, request)["status"] == "already_submitted"
    assert len(spawned) == 1
    argv, options = spawned[0]
    assert str(deployment / "source/scripts/remote_live.py") in argv
    assert options["start_new_session"] is True and len(options["pass_fds"]) == 1
    with pytest.raises(ValueError, match="different input"):
        LIVE["start"](root, deployment, request | {"seed": 19})


def test_unreconciled_previous_run_cannot_be_replaced(cloud):
    root, deployment, spawned = cloud
    with pytest.raises(ValueError, match="reconciliation"):
        LIVE["start"](root, deployment, {"action": "live_start", "run_id": "20260921T120001Z-0123abcd", "seed": 19})
    assert len(spawned) == 1


@pytest.mark.parametrize("changes", [{"run_id": "../elsewhere"}, {"seed": 0}, {"seed": True}, {"command": "land"}])
def test_start_rejects_unbounded_inputs(cloud, changes):
    root, deployment, spawned = cloud
    with pytest.raises(ValueError):
        LIVE["start"](root, deployment, {"action": "live_start", "run_id": RUN, "seed": 7, **changes})
    assert len(spawned) == 1


def test_pending_mailbox_is_not_overwritten_and_duplicate_does_not_extend_expiry(cloud):
    root, deployment, _ = cloud
    case = seed_runtime(root)
    result = LIVE["operate"](root, deployment, operation())
    assert result["status"] == "submitted"
    before = (case / "aircraft/operator.json").read_bytes()
    assert LIVE["operate"](root, deployment, operation())["status"] == "already_submitted"
    assert (case / "aircraft/operator.json").read_bytes() == before
    with pytest.raises(ValueError, match="unavailable"):
        LIVE["operate"](root, deployment, operation(request_id="another-123", operation="cancel"))
    with pytest.raises(ValueError, match="different input"):
        LIVE["operate"](root, deployment, operation(operation="cancel"))
    assert (case / "aircraft/operator.json").read_bytes() == before


@pytest.mark.parametrize("changes", [{"mission_id": "m-old"}, {"step_id": "takeoff"}, {"operation": "reset"}, {"request_id": "../escape"}])
def test_late_and_unsupported_operations_do_not_write_mailbox(cloud, changes):
    root, deployment, _ = cloud
    case = seed_runtime(root)
    with pytest.raises(ValueError):
        LIVE["operate"](root, deployment, operation(**changes))
    assert not (case / "aircraft/operator.json").exists()


def test_stale_observation_and_terminal_mission_remove_controls(cloud):
    root, _, _ = cloud
    case = seed_runtime(root)
    _, record = LIVE["live_records"](root, LIVE["owned_job"](root, RUN)[1])
    assert set(LIVE["available_actions"](record)[0]) == {"pause", "cancel"}
    original = copy.deepcopy(record)
    record["runtime"]["monotonic"] -= 2
    assert LIVE["available_actions"](record)[0] == []
    original["result"] = {"completed": True}
    assert LIVE["available_actions"](original)[0] == []
    path = case / "aircraft/executive.jsonl"
    with path.open("a") as output:
        output.write('{"kind":')
    assert len(LIVE["rows"](path)) == 1


def test_reads_do_not_create_state_or_control_files(tmp_path):
    deployment = tmp_path / "release"
    deployment.mkdir()
    (deployment / "manifest.json").write_text(json.dumps({"source_sha": "a" * 40}))
    before = set(tmp_path.rglob("*"))
    assert LIVE["snapshot"](tmp_path, deployment)["job"] is None
    assert set(tmp_path.rglob("*")) == before
