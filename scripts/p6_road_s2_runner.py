"""Record bounded whole-road calls inside the isolated cloud-only runner.

只在隔离云端执行器中录制有界完整道路调用。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from p6_road_s2 import (
    ARGUMENTS,
    RETRY_POLICY,
    VERSION,
    attempt_marker,
    digest,
    make_messages,
    prediction,
    verify_package,
    write,
)


async def record_run(package: Path, output: Path, provider, context: dict, *, source: str) -> dict:
    """Keep started markers, failed attempts and exactly one recorded answer per planned observation.

An attempt that produced no answer (timeout, transport or HTTP error) may be retried under the fixed
RETRY_POLICY; once any answer is recorded for an observation it is never asked again.

保留开始标记与失败尝试，每个计划观察恰好录制一个回答。没有得到回答的尝试（超时、传输或 HTTP 错误）
按固定 RETRY_POLICY 重试；某观察一旦录得回答，就不再重问。
"""
    from drone_agent.fleet.analysis_jobs import redact_images
    from drone_agent.providers.replay import RecordingProvider, ReplayProvider

    profile, inputs, plan = verify_package(package)
    if source not in {"recorded", "scripted"}:
        raise ValueError("invalid run source")
    if source == "recorded" and any(r["task_origin"] == "qa_fixture" for r in inputs["rows"]):
        raise ValueError("QA fixtures cannot enter a live model batch")
    output.mkdir(parents=True, exist_ok=False)
    (output / "recordings").mkdir()
    header = {"format": "drone.p6-road-s2-run/v1", "status": "running", "source": source,
              "provider_id": context["provider_id"], "model": profile["model"], "endpoint_host": context.get("endpoint_host", ""),
              "runtime_source_sha": context["runtime_source_sha"], "profile_sha256": plan["profile_sha256"],
              "eval_source_commit": context.get("eval_source_commit"),
              "inputs_sha256": plan["inputs_sha256"], "plan_sha256": digest(package / "plan.json"),
              "runner_sha256": digest(Path(__file__)), "planned_calls": plan["planned_calls"], "retry_policy": RETRY_POLICY,
              "attempts_started": 0, "failed_attempts": 0, "responses_recorded": 0,
              "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "formal_release_eligible": False}

    def progress():
        (output / "run.json").write_text(json.dumps(header, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    progress()
    unanswered_streak, last_start, results = 0, 0.0, []
    for sample in inputs["rows"]:
        ident = sample["sample_id"]
        messages = make_messages(profile, sample, package)
        path = output / "recordings" / (ident + ".json")
        result = {"sample_id": ident, "status": "failed", "source": source, "attempts": []}
        for attempt in range(1, RETRY_POLICY["max_attempts_per_observation"] + 1):
            if source == "recorded":
                wait = profile["minimum_start_interval_s"] - (time.monotonic() - last_start)
                if attempt > 1:
                    # Back off before re-asking an unanswered observation / 重问无回答观察前退避
                    wait = max(wait, RETRY_POLICY["backoff_s"] * (attempt - 1), result["attempts"][-1].get("retry_after_s") or 0)
                await asyncio.sleep(max(0, wait))
            write(output / attempt_marker(ident, attempt), {"sample_id": ident, "attempt": attempt, "source": source,
                  "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "plan_sha256": header["plan_sha256"]})
            header["attempts_started"] += 1
            progress()
            recorder = RecordingProvider(provider, redact=redact_images)
            last_start = time.monotonic()
            try:
                response = await asyncio.wait_for(recorder.complete(messages, **{k: profile[k] for k in ARGUMENTS}), timeout=profile["timeout_s"])
                content, used, finish, usage = response
                recording = recorder.recording(source=source, provider_id=context["provider_id"], model=profile["model"],
                    endpoint_host=context.get("endpoint_host", ""), prompt_version=VERSION, prompt_sha256=plan["profile_sha256"],
                    software_revision=context["runtime_source_sha"], label=ident)
                if path.exists():
                    raise ValueError("recording would be overwritten")
                recording.save(path)
                replay = ReplayProvider(recording)
                if await replay.complete(messages, **{k: profile[k] for k in ARGUMENTS}) != response or not replay.exhausted:
                    raise ValueError("strict replay differs")
                result.update(status="answered", attempt=attempt, model_used=used, finish=finish, usage=list(usage),
                              recording_sha256=digest(path), prediction=prediction(sample, content, used, finish, profile),
                              strict_replay="passed")
                header["responses_recorded"] += 1
                break
            except Exception as error:
                answered = path.exists()
                result["attempts"].append({"attempt": attempt, "error_type": type(error).__name__,
                                           "http_status": getattr(error, "status_code", None),
                                           "retry_after_s": getattr(error, "retry_after", None),
                                           "answer_recorded": answered,
                                           "duration_s": round(time.monotonic() - last_start, 3)})
                header["failed_attempts"] += 1
                if answered:
                    # An answer exists: never ask again / 已有回答：不再重问
                    result["status"] = "recorded_but_invalid"
                    break
        result["duration_s"] = round(time.monotonic() - last_start, 3)
        unanswered_streak = 0 if result["status"] == "answered" else unanswered_streak + 1
        write(output / (ident + ".result.json"), result)
        results.append(result)
        progress()
        print(json.dumps({"sample_id": ident, "status": result["status"], "done": len(results), "planned": len(inputs["rows"])}), flush=True)
        if unanswered_streak >= RETRY_POLICY["stop_after_unanswered_observations"]:
            break
    header.update(status="completed" if len(results) == len(inputs["rows"]) else "stopped_after_unanswered_observations",
                  responses_failed=sum(r["status"] != "answered" for r in results),
                  finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    progress()
    return header


async def main() -> None:
    """Require the frozen cloud invocation envelope before touching a provider.

接触模型之前要求冻结的云端调用外壳。
"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    args = parser.parse_args()
    if (sys.platform != "linux" or os.getenv("DRONE_P6_ROAD_CLOUD") != "v1"
            or not os.getenv("DRONE_SOURCE_SHA") or not os.getenv("DRONE_EVAL_SOURCE_SHA")
            or digest(args.package / "plan.json") != args.plan_sha256):
        raise ValueError("live model invocation requires the exact cloud-only envelope")
    profile, inputs, _ = verify_package(args.package)
    if any(r["task_origin"] == "qa_fixture" for r in inputs["rows"]):
        raise ValueError("QA tasks are software fixtures only")
    from drone_agent.providers import build_provider

    provider, config = build_provider("vision", model=profile["model"], guarded=False)
    result = await record_run(args.package, args.output, provider, {"provider_id": config.provider_id,
        "endpoint_host": config.endpoint_host, "runtime_source_sha": os.environ["DRONE_SOURCE_SHA"],
        "eval_source_commit": os.environ["DRONE_EVAL_SOURCE_SHA"]}, source="recorded")
    if result["status"] != "completed" or result["responses_failed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
