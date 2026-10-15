"""Record one model response per frozen view using the candidate contract.

按逐候选协议为每个冻结视图录制一次模型回答。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from p6_candidate_review import digest, input_file, make_messages, read, validate_answer, write


async def main() -> None:
    """Make bounded provider calls only, retaining every failure and recording.

只做有界模型调用，保留每次失败与录制。
"""
    from drone_agent.fleet.analysis_jobs import redact_images
    from drone_agent.providers import build_provider
    from drone_agent.providers.replay import Recording, RecordingProvider, ReplayProvider

    config, manifest, plan = read(Path("/config.json")), read(Path("/input/manifest.json")), read(Path("/plan.json"))
    for row in manifest:
        for item in row["images"]:
            input_file(Path("/input"), item)
    contract = Path(__file__).with_name("p6_candidate_review.py")
    if digest(contract) != plan["contract_sha256"] or digest(Path(__file__)) != plan["runner_sha256"]:
        raise ValueError("frozen contract or runner changed")
    out = Path("/output")
    (out / "recordings").mkdir(exist_ok=False)
    provider, pc = build_provider("vision", model=config["model"], guarded=False)
    header = {"status": "running", "model_requested": config["model"], "provider": pc.provider_id,
              "endpoint_host": pc.endpoint_host, "runtime_source_sha": os.environ["DRONE_SOURCE_SHA"],
              "source": "live_model_candidate_review", "prompt_version": config["prompt_version"],
              "config_sha256": digest(Path("/config.json")), "manifest_sha256": digest(Path("/input/manifest.json")),
              "runner_sha256": digest(Path(__file__)), "contract_sha256": digest(contract),
              "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "formal_release_eligible": False}
    # Progress is mutable; individual answers and recordings are retained once. / 进度可更新，逐项回答与录制只写一次。
    def progress(name, value):
        (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    progress("run.json", header)
    kwargs = {k: config[k] for k in ("model", "temperature", "max_tokens", "thinking", "reasoning_effort", "timeout_s")}
    results, failures, last_start = [], 0, 0.0
    for row in manifest:
        if (out / (row["case_id"] + ".json")).exists():
            raise ValueError("refusing duplicate view invocation")
        messages = make_messages(config, row, Path("/input"))
        await asyncio.sleep(max(0, config["minimum_start_interval_s"] - (time.monotonic() - last_start)))
        last_start = time.monotonic()
        recorder = RecordingProvider(provider, redact=redact_images)
        result = {"case_id": row["case_id"], "source": "live_model", "attempts": 1}
        write(out / (row["case_id"] + ".started.json"), {"started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        try:
            response = await asyncio.wait_for(recorder.complete(messages, **kwargs), timeout=config["timeout_s"])
            text, used, finish, usage = response
            recording = recorder.recording(source="recorded", provider_id=pc.provider_id, model=used,
                endpoint_host=pc.endpoint_host, prompt_version=config["prompt_version"],
                prompt_sha256=header["config_sha256"], software_revision=header["runtime_source_sha"], label=row["case_id"])
            path = out / "recordings" / (row["case_id"] + ".json")
            recording.save(path)
            replay = ReplayProvider(Recording.load(path))
            if await replay.complete(messages, **kwargs) != response or not replay.exhausted:
                raise ValueError("strict replay failed")
            value, issues = validate_answer(text, finish, row["proposals"])
            if used != config["model"]:
                issues.append("unexpected_model")
            result.update(status="answered", model_used=used, finish=finish, usage=list(usage), raw_content=text,
                          parsed=value, schema_issues=issues, strict_replay="passed", recording_sha256=digest(path))
            failures = 0
        except Exception as exc:
            result.update(status="failed", error_type=type(exc).__name__, http_status=getattr(exc, "status_code", None))
            failures += 1
        result["duration_s"] = round(time.monotonic() - last_start, 3)
        write(out / (row["case_id"] + ".json"), result)
        results.append(result)
        progress("results.json", results)
        print(json.dumps({"case_id": row["case_id"], "status": result["status"], "done": len(results), "of": len(manifest)}), flush=True)
        if failures >= 3:
            break
    header.update(status="completed" if len(results) == len(manifest) else "stopped_after_consecutive_failures",
                  cases_attempted=len(results), cases_answered=sum(r["status"] == "answered" for r in results),
                  cases_failed=sum(r["status"] == "failed" for r in results),
                  schema_valid=sum(r["status"] == "answered" and not r["schema_issues"] for r in results),
                  strict_replays=sum(r.get("strict_replay") == "passed" for r in results),
                  usage_input=sum(r.get("usage", [0, 0])[0] for r in results),
                  usage_output=sum(r.get("usage", [0, 0])[1] for r in results),
                  finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    progress("run.json", header)


if __name__ == "__main__":
    asyncio.run(main())
