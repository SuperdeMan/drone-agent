"""Fetching a recorded cloud run must verify every byte it copies and never widen the remote surface.

拉取云端记录的运行必须核对复制的每个字节，且不扩大远端可触达面。
"""

import argparse
import hashlib
import json
import runpy
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLIENT = runpy.run_path(str(ROOT / "scripts/dev_stack.py"))
REMOTE = CLIENT["REMOTE"]
DEPLOYMENT = "20260919T170015Z-8b7fc06e"
RUN = "m1-20260919T171218Z-db465377"
REMOTE_PATH = f"/home/ubuntu/drone-agent/artifacts/{DEPLOYMENT}/{RUN}"


def workspace(tmp_path: Path) -> Path:
    """A fake cloud workspace holding one run with two cases and a receipt. / 含一次运行、两个用例与回执的伪云端工作区。"""
    root = tmp_path / "ws"
    run = root / "artifacts" / DEPLOYMENT / RUN
    rows = []
    for case in ("nominal-7", "cancel_cruise-7"):
        for folder in ("input", "aircraft", "ulog", "judge"):
            (run / case / folder).mkdir(parents=True)
        scenario = {"scenario": {"id": case.rsplit("-", 1)[0], "expected": "completed"}, "seed": 7, "source_sha": "a" * 40}
        (run / case / "input/scenario.json").write_text(json.dumps(scenario))
        (run / case / "aircraft/status.json").write_text(json.dumps({"observation": {"in_air": False, "armed": False}}))
        (run / case / "ulog/flight.ulg").write_bytes(b"ULog" + bytes(range(64)))
        (run / case / "judge/result.json").write_text(json.dumps({"classification": "completed", "passed": True}))
        artifacts = {p.relative_to(run / case).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted((run / case).rglob("*")) if p.is_file() and "judge" not in p.parts}
        rows.append({"scenario": scenario["scenario"]["id"], "seed": 7, "passed": True, "classification": "completed",
                     "artifacts": artifacts})
    (run / "progress.json").write_text(json.dumps({"source_sha": "a" * 40, "results": rows, "artifact_directory": REMOTE_PATH}))
    return root


def fake_transport(root: Path, *, tamper: dict | None = None):
    """Serve status/inspect from the fake workspace, presenting the canonical remote path.

    用伪工作区响应 status/inspect，并给出标准的远端路径。
    """

    def transport(_connection, request):
        if request["action"] == "status":
            return {"deployed": True, "deployment_id": DEPLOYMENT}
        assert request["action"] == "inspect"
        listing = REMOTE["inspect_run"](root, request)
        listing["path"] = (tamper or {}).get("path", REMOTE_PATH)
        for case, name in (tamper or {}).get("rename", []):
            listing["cases"][name] = listing["cases"].pop(case)
        return listing

    return transport


def fake_download(root: Path, *, corrupt: str | None = None):
    def download(_connection, transfers):
        for remote, local in transfers:
            assert remote.startswith(REMOTE_PATH + "/")
            source = root / "artifacts" / DEPLOYMENT / RUN / remote[len(REMOTE_PATH) + 1:]
            local.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, local)
            if corrupt and remote.endswith(corrupt):
                local.write_bytes(b"tampered")

    return download


def arguments(tmp_path: Path, **overrides) -> argparse.Namespace:
    values = dict(run=RUN, deployment=DEPLOYMENT, cases="all", exclude="", receipt=None,
                  artifacts=tmp_path / "local", apply=False, no_viewer=False)
    values.update(overrides)
    return argparse.Namespace(**values)


def test_remote_listing_is_read_only_and_scoped(tmp_path):
    root = workspace(tmp_path)
    runs = REMOTE["artifact_runs"](root)["runs"]
    assert runs == [{"deployment_id": DEPLOYMENT, "run": RUN, "source_sha": "a" * 40, "cases": ["nominal-7", "cancel_cruise-7"],
                     "passed": 2, "total": 2, "has_receipt": True}]
    listing = REMOTE["inspect_run"](root, {"deployment": DEPLOYMENT, "run": RUN, "cases": ["nominal-7"]})
    assert set(listing["cases"]) == {"nominal-7"} and listing["receipt"]["source_sha"] == "a" * 40
    entry = listing["cases"]["nominal-7"]["ulog/flight.ulg"]
    assert entry["sha256"] == hashlib.sha256(b"ULog" + bytes(range(64))).hexdigest() and entry["size"] == 68
    for request in ({"deployment": "../etc", "run": RUN}, {"deployment": DEPLOYMENT, "run": "m1-../x"},
                    {"deployment": DEPLOYMENT, "run": RUN, "cases": ["../nominal-7"]}, {"deployment": DEPLOYMENT, "run": RUN, "cases": "nominal-7"}):
        with pytest.raises(ValueError):
            REMOTE["inspect_run"](root, request)
    with pytest.raises(ValueError):
        REMOTE["dispatch"]({"action": "inspect", "deployment": DEPLOYMENT, "run": "m9-20260101T000000Z-00000000"})


def test_fetch_plans_before_copying_anything(tmp_path):
    root = workspace(tmp_path)
    result = CLIENT["fetch_command"](None, arguments(tmp_path), transport=fake_transport(root), download=fake_download(root))
    assert result["status"] == "plan" and result["files"] == 8 and result["cases"] == ["cancel_cruise-7", "nominal-7"]
    assert result["bytes"] > 0 and result["source_sha"] == "a" * 40
    assert not (tmp_path / "local").exists()


def test_fetch_verifies_digests_and_builds_the_viewer(tmp_path):
    root = workspace(tmp_path)
    args = arguments(tmp_path, apply=True, exclude="ulog", cases="nominal-7", receipt=root / "artifacts" / DEPLOYMENT / RUN / "progress.json")
    result = CLIENT["fetch_command"](None, args, transport=fake_transport(root), download=fake_download(root))
    local = Path(result["local_directory"])
    assert result["status"] == "fetched" and result["mismatched"] == [] and result["archived_receipt_agrees"] is True
    assert local == (tmp_path / "local/runs" / DEPLOYMENT / RUN).resolve()
    assert (local / "nominal-7/input/scenario.json").is_file() and not (local / "nominal-7/ulog").exists()
    assert not (local / "cancel_cruise-7").exists()
    assert json.loads((local / "receipt.json").read_text())["source_sha"] == "a" * 40
    fetched = json.loads((local / "fetch.json").read_text())
    assert fetched["status"] == "fetched" and set(fetched["digests"]) == {"nominal-7/aircraft/status.json", "nominal-7/input/scenario.json", "nominal-7/judge/result.json"}
    assert Path(result["viewer"]) == local / "viewer.html" and "record-data" in (local / "viewer.html").read_text(encoding="utf-8")


def test_fetch_refuses_bytes_that_differ_from_the_recorded_digests(tmp_path):
    root = workspace(tmp_path)
    with pytest.raises(RuntimeError, match="differ"):
        CLIENT["fetch_command"](None, arguments(tmp_path, apply=True), transport=fake_transport(root),
                                download=fake_download(root, corrupt="aircraft/status.json"))
    fetched = json.loads((tmp_path / "local/runs" / DEPLOYMENT / RUN / "fetch.json").read_text())
    assert fetched["status"] == "digest_mismatch"
    assert fetched["mismatched"] == ["cancel_cruise-7/aircraft/status.json", "nominal-7/aircraft/status.json"]
    assert not (tmp_path / "local/runs" / DEPLOYMENT / RUN / "viewer.html").exists()


@pytest.mark.parametrize("defect", ["run", "deployment", "case", "path", "rename", "ascii"])
def test_fetch_rejects_unsafe_references(tmp_path, defect):
    root = workspace(tmp_path)
    args = arguments(tmp_path)
    tamper = None
    if defect == "run":
        args.run = "m1-../../releases"
    elif defect == "deployment":
        args.deployment = "../car-agent"
    elif defect == "case":
        args.cases = "nominal-7,../escape"
    elif defect == "path":
        tamper = {"path": "/home/ubuntu/car-agent/artifacts/" + DEPLOYMENT + "/" + RUN}
    elif defect == "rename":
        tamper = {"rename": [("nominal-7", "../nominal-7")]}
    else:
        args.artifacts = tmp_path / "证据"
    with pytest.raises(ValueError):
        CLIENT["fetch_command"](None, args, transport=fake_transport(root, tamper=tamper), download=fake_download(root))


def test_archived_receipt_disagreement_is_reported(tmp_path):
    root = workspace(tmp_path)
    archived = json.loads((root / "artifacts" / DEPLOYMENT / RUN / "progress.json").read_text())
    archived["source_sha"] = "b" * 40
    (tmp_path / "archived.json").write_text(json.dumps(archived))
    result = CLIENT["fetch_command"](None, arguments(tmp_path, receipt=tmp_path / "archived.json"),
                                     transport=fake_transport(root), download=fake_download(root))
    assert result["archived_receipt_agrees"] is False
