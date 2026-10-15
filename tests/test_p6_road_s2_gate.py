"""Model-only cloud execution and final admission must fail closed on missing evidence.

纯模型云端执行及最终准入在证据缺失时必须保留失败。
"""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    remote = importlib.import_module("p6_road_s2_remote")
    cloud = importlib.import_module("p6_road_s2_cloud")
    gate = importlib.import_module("verify_p6_road_business")
    monkeypatch.setattr(remote.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(remote.os, "getgid", lambda: 1000, raising=False)
    return remote, cloud, gate


def setup_run(tmp_path, remote):
    root, folder = tmp_path / "project", tmp_path / "project/artifacts/p6-road-s2-20261015T000000Z"
    folder.mkdir(parents=True)
    plan = {"runtime_source_sha": "6" * 40, "runtime_image": remote.SHARED["IMAGE"], "eval_source_commit": "a" * 40,
            "data_plan_sha256": "b" * 64, "maximum_wall_seconds": 60}
    (folder / "execution-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (folder / "input").mkdir()
    (folder / "input/plan.json").write_text(json.dumps({"split": "development", "test_exposure_keys": []}), encoding="utf-8")
    return root, folder, plan


def test_busy_project_lock_never_starts_a_container_or_provider(modules, monkeypatch, tmp_path):
    remote, _, _ = modules
    root, folder, plan = setup_run(tmp_path, remote)
    monkeypatch.setitem(remote.SHARED, "try_lock", lambda r: None)

    def forbidden():
        raise AssertionError("preflight must not run while lock is busy")

    monkeypatch.setitem(remote.SHARED, "preflight", forbidden)
    receipt = remote.execute(root, folder, plan)
    assert receipt["status"] == "blocked_lock_busy" and receipt["attempts_started"] == 0
    assert receipt["container_started"] is False and not (folder / "output").exists()


def test_existing_or_ambiguous_execution_is_not_repeated(modules, tmp_path):
    remote, _, _ = modules
    root, folder, plan = setup_run(tmp_path, remote)
    (folder / "output").mkdir()
    with pytest.raises(ValueError, match="cannot be repeated"):
        remote.execute(root, folder, plan)


@pytest.mark.parametrize("post_failure", [False, True])
def test_environment_change_or_missing_postcheck_never_qualifies(modules, monkeypatch, tmp_path, post_failure):
    remote, _, _ = modules
    root, folder, plan = setup_run(tmp_path, remote)
    closed = []
    monkeypatch.setitem(remote.SHARED, "try_lock", lambda r: SimpleNamespace(close=lambda: closed.append(True)))
    monkeypatch.setitem(remote.SHARED, "preflight", lambda: {"image": plan["runtime_image"]})
    snapshots = iter([[{"id": "before"}], [{"id": "after"}]])

    def identities():
        value = next(snapshots)
        if post_failure and value[0]["id"] == "after":
            raise RuntimeError("synthetic postcheck failure")
        return value

    monkeypatch.setitem(remote.SHARED, "identities", identities)
    monkeypatch.setattr(remote.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    receipt = remote.execute(root, folder, plan)
    assert receipt["status"] == ("not_qualified_environment_check_failed" if post_failure else "not_qualified_environment_changed")
    assert closed == [True] and not receipt["formal_release_eligible"]


def test_container_has_only_model_mounts_and_no_control_access(modules, tmp_path):
    remote, _, _ = modules
    root, folder, plan = setup_run(tmp_path, remote)
    args = remote.container_command(root, folder, plan)
    assert "--read-only" in args and "--privileged" not in args
    assert args[args.index("--network") + 1] == "drone-agent-cloud_desk_model"
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    assert len(mounts) == 7 and all("m2-model" in m or str(folder) in m for m in mounts)
    assert next(m for m in mounts if "minimax.key" in m).endswith(":ro")
    assert not any("docker.sock" in m or "guardian" in m or "devices" in m for m in mounts)


@pytest.mark.parametrize("name", ["../other", "p6-road-s2-20261015T000000Z/../other", "car-agent", "p6-candidate-review-dev-20261015T000000Z"])
def test_status_and_fetch_cannot_target_other_artifacts(modules, name):
    with pytest.raises(ValueError, match="run id"):
        modules[1].remote_path(name)


def test_missing_real_evidence_cannot_close_wp01(modules, tmp_path, monkeypatch):
    gate = modules[2]
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"format": "drone.p6-road-business-evidence/v1", "candidate_commit": None, "paths": {}}), encoding="utf-8")

    async def forbidden(*args):
        raise AssertionError("live evidence must not be inferred without required files")

    monkeypatch.setattr(gate, "live_evidence", forbidden)
    result = asyncio.run(gate.judge(evidence, tmp_path))
    assert result["status"] == "missing" and not result["wp_p6_01_closed"] and not result["formal_release_eligible"]
    assert result["field_clearance_effect"] == "not_verified_outside_this_gate"


def test_evidence_paths_cannot_escape_the_declared_root(modules, tmp_path):
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"format": "drone.p6-road-business-evidence/v1", "candidate_commit": "a" * 40,
                                    "paths": {"manifest": "../outside.json"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="outside data root"):
        asyncio.run(modules[2].judge(evidence, tmp_path))


def test_changing_run_directory_does_not_allow_reusing_test_images(modules, tmp_path):
    remote, _, _ = modules
    root, first, plan = setup_run(tmp_path, remote)
    keys = ["image_" + "a" * 64, "group_" + "b" * 64]
    (first / "input/plan.json").write_text(json.dumps({"split": "test", "test_exposure_keys": keys}), encoding="utf-8")
    claim = remote.reserve_test_scope(root, first, plan)
    assert claim["required"] is True and claim["reserved_keys"] == 2
    second = root / "artifacts/p6-road-s2-20261015T010000Z"
    (second / "input").mkdir(parents=True)
    (second / "input/plan.json").write_text(json.dumps({"split": "test", "test_exposure_keys": keys}), encoding="utf-8")
    with pytest.raises(ValueError, match="already reserved"):
        remote.reserve_test_scope(root, second, plan)
    assert not (second / "test-exposure.json").exists()
