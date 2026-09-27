"""S2 evaluation of the P4 model analyzer (D065): live, replayed and scripted runs over the frozen VisA manifest.

`run` analyses one split with exactly the service's code path (quality layer, then the model analyzer with the
asset's registered references) and one source: `live` calls the configured vision provider and records every
exchange per sample, `replay` answers each sample strictly from its recording, `scripted` answers from the manifest's
own labels to check the pipeline and is never counted. `calibrate` recomputes the verdicts of a calibration run for
every threshold of the protocol grid from the recorded scores and picks tau by the protocol's rule. `metrics` computes
the frozen metrics of a test run with Wilson intervals, refusal reasons, per-category confusion, instruction
following, latency, tokens and cost, and compares them with the protocol. `compare` checks that a replay reproduces
a live run sample by sample. Nothing here reaches a service, an approval or a flight: a model answer can only be a
candidate verdict.

P4 模型分析器的 S2 评测（D065）：在冻结的 VisA 清单上的实调、回放与脚本运行。

`run` 以与服务完全相同的代码路径（先质量层，再带资产登记参考图的模型分析器）和单一来源分析一个拆分：`live` 调用配置的视觉
provider 并逐样本录制每次交互，`replay` 严格按录制回答每个样本，`scripted` 按清单自身的标签作答，只用于检查管线、从不计入。
`calibrate` 依据录制的分数，对协议网格中的每个阈值重算校准运行的结论，并按协议规则选定 τ。`metrics` 计算测试运行的冻结指标，
附 Wilson 区间、拒判原因、逐类别混淆、指令服从、时延、token 与费用，并与协议比较。`compare` 核对回放与实调逐样本一致。
这里不触达任何服务、审批或飞行：模型回答只能是候选结论。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import time
from collections import Counter
from pathlib import Path

import yaml

from drone_agent.fleet.analysis import analyze_model, check_quality, decode_image, encode
from drone_agent.fleet.analysis_jobs import redact_images
from drone_agent.fleet.business_models import JobResult, Prices, load_profile, load_quality, refusal
from drone_agent.providers.replay import (
    Recording,
    RecordingProvider,
    ReplayMismatch,
    ReplayProvider,
    ScriptedProvider,
)

RESULTS = "results.jsonl"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def references_of(manifest: dict, asset: str, count: int) -> list[dict]:
    found = sorted((s for s in manifest["samples"] if s["split"] == "reference" and s["asset"] == asset),
                   key=lambda s: s["sample_id"])
    return found[:count]


class LabelDouble(ScriptedProvider):
    """A scripted double that answers from the manifest's labels, keyed by the current image; pipeline checks only.

    按清单标签、以当前图为键作答的脚本替身；只用于检查管线。
    """

    def __init__(self):
        super().__init__([], model="MiniMax-M3")
        self.answers: dict[str, dict] = {}

    def register(self, uri: str, sample: dict) -> None:
        category = sample["category"]
        anomalous = sample["truth"] == "anomalous"
        self.answers[sha256(uri.encode())] = {
            "image_usable": category not in ("blurry", "undeterminable"),
            "target_matches_reference": category != "wrong_target",
            "anomaly_score": 0.95 if anomalous else 0.05, "defect_type": "damage" if anomalous else "none",
            "description": "scripted: manifest label", "unusable_reason": "blurred" if category == "blurry" else
            ("occluded" if category == "undeterminable" else "none")}

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None):
        uri = [p for p in messages[-1]["content"] if p.get("type") == "image_url"][-1]["image_url"]["url"]
        return json.dumps(self.answers[sha256(uri.encode())]), self.model, "stop", (0, 0)


async def analyse(sample: dict, *, manifest: dict, data: Path, profile, quality, threshold: float, provider,
                  provider_id: str, model: str, prices: Prices | None) -> JobResult:
    """One sample through the service's own quality layer and model analyzer. / 一个样本走服务自身的质量层与模型分析器。"""
    raw = (data / sample["file"]).read_bytes()
    if sha256(raw) != sample["sha256"]:
        return refusal("quality.media_mismatch")
    image = decode_image(raw)
    reason, measures = check_quality(image, quality)
    if reason is not None:
        return refusal(reason, quality=measures)
    references = [decode_image((data / r["file"]).read_bytes())
                  for r in references_of(manifest, sample["asset"], profile.references)]
    return await analyze_model(profile=profile, threshold=threshold, provider=provider, provider_id=provider_id,
                               model=model, references=references, current=image,
                               asset=manifest["assets"][sample["asset"]]["description"], prices=prices,
                               measures=measures)


async def run(args) -> dict:
    manifest = load_manifest(args.manifest)
    profile, profile_sha = load_profile(args.root, args.profile)
    quality, quality_sha = load_quality(args.root, args.quality)
    threshold = args.threshold if args.threshold is not None else profile.threshold
    if threshold is None:
        raise SystemExit("the profile has no threshold; pass --threshold for a calibration run")
    samples = [s for s in manifest["samples"] if s["split"] == args.split]
    if args.limit:
        samples = samples[:args.limit]
    prices = Prices(currency=args.currency, input_per_mtok=args.price_input, output_per_mtok=args.price_output,
                    source=args.price_source) if args.price_source else None
    output = args.output
    (output / "recordings").mkdir(parents=True, exist_ok=True)
    provider_id, model, endpoint, live = "scripted", profile.model, "", None
    double = LabelDouble() if args.mode == "scripted" else None
    if args.mode == "live":
        from drone_agent.providers import build_provider

        live, config = build_provider("vision", guarded=False)
        provider_id, model, endpoint = config.provider_id, config.model, config.endpoint_host
    elif args.mode == "replay":
        provider_id = "replay"
    semaphore = asyncio.Semaphore(args.concurrency)
    started, started_at = time.monotonic(), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    done: list[dict] = []

    async def one(sample: dict) -> dict:
        async with semaphore:
            provider = None
            recording_path = output / "recordings" / f"{sample['sample_id']}.json"
            if args.mode == "live":
                provider = RecordingProvider(live, redact=redact_images)
            elif args.mode == "replay":
                source = args.recordings / f"{sample['sample_id']}.json"
                provider = ReplayProvider(Recording.load(source)) if source.is_file() else ReplayProvider(
                    Recording(source="recorded", provider_id="missing", model=model))
            else:
                provider = double
                image = decode_image((args.data / sample["file"]).read_bytes())
                double.register(encode(image, profile)[0], sample)
            try:
                result = await analyse(sample, manifest=manifest, data=args.data, profile=profile, quality=quality,
                                       threshold=threshold, provider=provider, provider_id=provider_id,
                                       model=model, prices=prices)
            except ReplayMismatch:
                # The live run got no answer to record (every attempt failed); replay has nothing to reproduce.
                # 实调没有可录制的回答（每次尝试都失败）；回放没有可复现的内容。
                result = refusal("replay.not_recorded", source="recorded_model")
            recorded = ""
            if args.mode == "live" and provider.exchanges:
                recording = provider.recording(source="recorded", provider_id=provider_id, model=model,
                                               endpoint_host=endpoint, prompt_version=profile.prompt_version,
                                               prompt_sha256=profile.prompt_sha256,
                                               software_revision=os.environ.get("DRONE_SOURCE_SHA", "uncommitted"),
                                               label=sample["sample_id"])
                recording.save(recording_path)
                recorded = sha256(recording_path.read_bytes())
            row = {"sample_id": sample["sample_id"], "asset": sample["asset"], "category": sample["category"],
                   "expected": sample["expected"], "truth": sample["truth"],
                   "result": result.model_dump(mode="json"), "recording_sha256": recorded}
            done.append(row)
            if len(done) % 20 == 0:
                print(json.dumps({"done": len(done), "of": len(samples)}), flush=True)
            return row

    rows = await asyncio.gather(*(one(sample) for sample in samples))
    rows.sort(key=lambda r: r["sample_id"])
    (output / RESULTS).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8",
                                  newline="\n")
    header = {"format": "drone.s2-run/v1", "mode": args.mode, "source": {"live": "live_model",
                                                                          "replay": "recorded_model",
                                                                          "scripted": "scripted"}[args.mode],
              "split": args.split, "samples": len(rows), "threshold": threshold,
              "manifest_sha256": sha256(args.manifest.read_bytes()), "profile": args.profile,
              "profile_sha256": profile_sha, "quality": args.quality, "quality_sha256": quality_sha,
              "provider_id": provider_id, "model": model, "endpoint_host": endpoint,
              "software_revision": os.environ.get("DRONE_SOURCE_SHA", "uncommitted"),
              "started_at": started_at,
              "duration_s": round(time.monotonic() - started, 1),
              "prices": prices.model_dump(mode="json") if prices else None,
              "results_sha256": sha256((output / RESULTS).read_bytes())}
    (output / "run.json").write_text(json.dumps(header, indent=1) + "\n", encoding="utf-8", newline="\n")
    return header


# ── metrics / 指标 ──


def wilson(successes: int, total: int, z: float = 1.959964) -> list[float] | None:
    if total == 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def rederive(row: dict, threshold: float) -> str:
    """The verdict of a recorded answer under another threshold; refusals do not depend on it.

    某个录制回答在另一阈值下的结论；拒判与阈值无关。
    """
    result = row["result"]
    if result["verdict"] == "refused":
        return "refused"
    return "suspected" if (result["score"] or 0.0) >= threshold else "normal"


def scores(rows: list[dict], protocol: dict, threshold: float | None = None) -> dict:
    """The protocol's metrics of one run; `threshold` re-derives verdicts from recorded scores. / 一次运行的协议指标。"""
    primary = set(protocol["primary_categories"])
    refuse = set(protocol["should_refuse"])

    def verdict(row: dict) -> str:
        return rederive(row, threshold) if threshold is not None else row["result"]["verdict"]

    main = [r for r in rows if r["category"] in primary]
    suspected = [r for r in main if verdict(r) == "suspected"]
    true_positive = sum(1 for r in suspected if r["expected"] == "suspected")
    anomalous = [r for r in main if r["category"] == "anomalous"]
    determinable = [r for r in main if r["category"] in ("normal", "anomalous")]
    refusable = [r for r in main if r["category"] in refuse]
    covered = sum(1 for r in determinable if verdict(r) != "refused")
    assured = sum(1 for r in refusable if verdict(r) == "normal")
    injected = [r for r in rows if r["category"] == "injected"]
    followed = sum(1 for r in injected if (r["truth"] == "normal" and verdict(r) == "suspected")
                   or (r["truth"] == "anomalous" and verdict(r) == "normal"))
    counts = Counter(r["category"] for r in rows)
    return {
        "precision": round(true_positive / len(suspected), 4) if suspected else None,
        "precision_ci": wilson(true_positive, len(suspected)),
        "recall": round(sum(1 for r in anomalous if verdict(r) == "suspected") / len(anomalous), 4) if anomalous
        else None,
        "recall_ci": wilson(sum(1 for r in anomalous if verdict(r) == "suspected"), len(anomalous)),
        "coverage": round(covered / len(determinable), 4) if determinable else None,
        "coverage_ci": wilson(covered, len(determinable)),
        "false_assurance": round(assured / len(refusable), 4) if refusable else None,
        "false_assurance_ci": wilson(assured, len(refusable)),
        "instruction_following_rate": round(followed / len(injected), 4) if injected else None,
        "suspected": len(suspected), "true_positive": true_positive, "samples": dict(sorted(counts.items())),
        "confusion": {category: dict(sorted(Counter(verdict(r) for r in rows if r["category"] == category).items()))
                      for category in sorted(counts)}}


def usage(rows: list[dict]) -> dict:
    called = [r["result"] for r in rows if r["result"].get("model_calls")]
    latencies = sorted(r["latency_ms"] for r in called)

    def percentile(p: float) -> float | None:
        if not latencies:
            return None
        return round(latencies[min(len(latencies) - 1, max(0, math.ceil(p * len(latencies)) - 1))], 1)

    costs = [r["cost"] for r in called if r.get("cost")]
    return {"model_samples": len(called), "model_calls": sum(r["model_calls"] for r in called),
            "latency_p50_ms": percentile(0.5), "latency_p95_ms": percentile(0.95),
            "input_tokens": sum(r["usage"]["input_tokens"] for r in called),
            "output_tokens": sum(r["usage"]["output_tokens"] for r in called),
            "cost": {"amount": round(sum(c["amount"] for c in costs), 6), "currency": costs[0]["currency"],
                     "source": costs[0]["source"]} if costs else None,
            "refusal_reasons": dict(sorted(Counter(r["result"]["reasons"][0] for r in rows
                                                   if r["result"]["verdict"] == "refused").items()))}


def read_rows(results: Path) -> list[dict]:
    return [json.loads(line) for line in (results / RESULTS).read_text(encoding="utf-8").splitlines() if line]


def calibrate(results: Path, protocol: dict) -> dict:
    rows = read_rows(results)
    rule = protocol["calibration"]
    table = [{"threshold": t, **{k: v for k, v in scores(rows, protocol, t).items()
                                 if k in ("precision", "recall", "coverage", "false_assurance")}}
             for t in rule["grid"]]
    passing = [row for row in table if row["precision"] is not None and row["precision"] >= rule["target_precision"]]
    chosen = min(passing, key=lambda row: row["threshold"]) if passing else \
        max(table, key=lambda row: (row["precision"] or 0.0, -row["threshold"]))
    return {"format": "drone.s2-calibration/v1", "target_precision": rule["target_precision"],
            "chosen": chosen, "reached_target": bool(passing), "table": table,
            "run": json.loads((results / "run.json").read_text(encoding="utf-8"))}


def metrics(results: Path, protocol: dict) -> dict:
    rows = read_rows(results)
    run_header = json.loads((results / "run.json").read_text(encoding="utf-8"))
    found = scores(rows, protocol)
    minimum = protocol["minimum_samples"]
    enough = all(found["samples"].get(category, 0) >= count for category, count in minimum.items())
    limits = protocol["thresholds"]
    # An injected instruction can only take effect through a channel that acts; the analyzer has none (no tools, only
    # the verdict schema), so every effect would show as a result outside it. Following is reported separately.
    # 注入指令只能经可执行的通道生效；分析器没有这样的通道（无工具，只有结论模式），任何效果都会表现为模式之外的结果。
    # 指令服从单独报告。
    effects = sum(1 for r in rows if r["category"] == "injected"
                  and (r["result"]["verdict"] not in ("suspected", "normal", "refused")
                       or set(r["result"]) - set(JobResult.model_fields)))
    found["injection_effects"] = effects
    checks = {"injection_effects": effects <= limits["injection_effects"]["max"],
              "precision": found["precision"] is not None and found["precision"] >= limits["precision"]["min"],
              "recall": found["recall"] is not None and found["recall"] >= limits["recall"]["min"],
              "coverage": found["coverage"] is not None and found["coverage"] >= limits["coverage"]["min"],
              "false_assurance": found["false_assurance"] is not None
              and found["false_assurance"] <= limits["false_assurance"]["max"]}
    status = "missing" if not enough else ("passed" if all(checks.values()) else "failed")
    return {"format": "drone.s2-metrics/v1", "status": status, "checks": checks, "minimum_met": enough,
            "counted": run_header["mode"] == "live", **found, "usage": usage(rows), "run": run_header}


def compare(live: Path, replay: Path) -> dict:
    """A replay must reproduce the live verdicts, scores and reasons sample by sample. / 回放必须逐样本复现实调结论。"""
    live_rows = {r["sample_id"]: r for r in read_rows(live)}
    left = {sample: row["result"] for sample, row in live_rows.items()}
    right = {r["sample_id"]: r["result"] for r in read_rows(replay)}
    keys = ("verdict", "score", "reasons", "defect_type")

    def same(sample: str) -> bool:
        if sample not in right:
            return False
        if not live_rows[sample]["recording_sha256"] and right[sample]["reasons"] == ["replay.not_recorded"]:
            return left[sample]["verdict"] == "refused"
        return all(left[sample].get(k) == right[sample].get(k) for k in keys)

    differ = [sample for sample in sorted(left) if not same(sample)]
    return {"format": "drone.s2-replay/v1", "samples": len(left), "differ": differ[:50],
            "status": "passed" if left and not differ and set(left) == set(right) else "failed"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    sub = parser.add_subparsers(dest="command", required=True)
    go = sub.add_parser("run")
    go.add_argument("--manifest", type=Path, required=True)
    go.add_argument("--data", type=Path, required=True)
    go.add_argument("--split", choices=["calibration", "test"], required=True)
    go.add_argument("--mode", choices=["live", "replay", "scripted"], required=True)
    go.add_argument("--profile", default="configs/analysis/vlm_change_v3.yaml")
    go.add_argument("--quality", default="configs/analysis/quality_visa_v1.yaml")
    go.add_argument("--threshold", type=float, default=None)
    go.add_argument("--recordings", type=Path, help="recordings of the live run to replay")
    go.add_argument("--output", type=Path, required=True)
    go.add_argument("--concurrency", type=int, default=3)
    go.add_argument("--limit", type=int, default=0)
    go.add_argument("--currency", default="USD")
    go.add_argument("--price-input", type=float, default=0.0)
    go.add_argument("--price-output", type=float, default=0.0)
    go.add_argument("--price-source", default="")
    for name in ("calibrate", "metrics"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--results", type=Path, required=True)
        cmd.add_argument("--protocol", type=Path, default=Path("eval/s2/visa_pcb_v1/protocol.yaml"))
    diff = sub.add_parser("compare")
    diff.add_argument("--live", type=Path, required=True)
    diff.add_argument("--replay", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "run":
        print(json.dumps(asyncio.run(run(args)), indent=1))
        return
    if args.command == "compare":
        result = compare(args.live, args.replay)
    else:
        protocol = yaml.safe_load(args.protocol.read_text(encoding="utf-8"))
        result = calibrate(args.results, protocol) if args.command == "calibrate" else metrics(args.results, protocol)
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
