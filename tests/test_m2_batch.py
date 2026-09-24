"""Cloud M2 batches stop at the first failure while preserving evidence. / 云端 M2 批次首次失败即停并保留证据。"""

import json
import runpy
from pathlib import Path


def test_m2_failure_stops_new_cases_and_restores_idle(tmp_path, monkeypatch):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/remote_m2.py"))
    run = module["run_m2"]
    space = run.__globals__
    deployment = tmp_path / "releases" / "test"
    deployment.mkdir(parents=True)
    (deployment / "manifest.json").write_text(json.dumps({"source_sha": "a" * 40, "control_sha256": "b" * 64}))
    (tmp_path / "secrets").mkdir()
    images = {"ground": "ground", "aircraft": "aircraft", "sim": "sim"}
    calls, restored = [], []
    monkeypatch.setitem(space, "build_images", lambda *_: images)
    monkeypatch.setitem(space, "provision", lambda *_: {"signer_key_id": "test", "fingerprints": {}})

    def case(*args):
        calls.append((args[-2]["id"], args[-1]))
        return {"passed": len(calls) == 1, "scenario": args[-2]["id"], "seed": args[-1]}

    monkeypatch.setitem(space, "run_case", case)
    monkeypatch.setitem(space, "HELPERS", {
        "foreign_identity": lambda: {"sha256": "unchanged"},
        "capacity": lambda: {"memory": {"MemAvailable": 4 * 1024**3}},
        "run": lambda *_: json.dumps({"scenarios": [{"id": "first"}, {"id": "second"}], "seeds": [7, 19, 41]}),
        "compose": lambda *_: restored.append(True), "inspect_image": lambda name: {"Id": name},
    })
    result = run(tmp_path, deployment, {"run_id": "batch", "scenario": "all", "seeds": [7, 19, 41]})
    assert calls == [("first", 7), ("first", 19)]
    assert result["status"] == "failed" and restored == [True]
    saved = json.loads((tmp_path / "artifacts/test/m2-batch/suite.json").read_text())
    assert saved["results"] == result["results"]
