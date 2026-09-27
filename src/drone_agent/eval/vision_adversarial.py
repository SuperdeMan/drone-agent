"""Run the vision-answer adversarial corpus through the service's own model analyzer (P4, D063 §4).

Each case's raw answer comes from a scripted double (never reported as model behaviour) and goes through
`analyze_model` with the change-v1 profile and the corpus threshold on neutral images. A case passes when the
outcome is exactly the expected candidate verdict or refusal reason. `escapes` counts hostile answers that produced a
verdict where a refusal was expected; every outcome is a candidate or a refusal by construction, never an action.

用服务自身的模型分析器运行视觉回答对抗语料（P4，D063 §4）。每个用例的原始回答来自脚本替身（从不当作模型行为报告），
经 `analyze_model`、change-v1 画像与语料阈值在中性图像上处理。结果恰为期望的候选结论或拒判原因时用例通过。`escapes`
统计本应拒判却产生结论的恶意回答；按构造，每个结果都只是候选或拒判，从不是动作。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

import numpy as np
import yaml

from drone_agent.fleet.analysis import analyze_model
from drone_agent.fleet.business_models import load_profile
from drone_agent.providers.replay import ScriptedProvider

CORPUS = "eval/adversarial/vision_v1.yaml"


def raw_answer(base: dict, case: dict) -> str:
    """The exact text the double returns for one case. / 替身为一个用例返回的确切文本。"""
    if "raw" in case:
        return case["raw"]
    answer = {**base, **case.get("answer", {})}
    for key in case.get("drop", []):
        answer.pop(key, None)
    if case.get("long_description"):
        answer["description"] = "x" * case["long_description"]
    text = json.dumps(answer)
    if case.get("fence"):
        text = f"```json\n{text}\n```"
    return case.get("prefix", "") + text + case.get("suffix", "")


async def run_corpus(root: Path, path: str = CORPUS) -> dict:
    corpus = yaml.safe_load((root / path).read_text(encoding="utf-8"))
    if corpus.get("format") != "drone.vision-adversarial/v1":
        raise ValueError("not a drone.vision-adversarial/v1 corpus")
    profile, profile_sha = load_profile(root, corpus["profile"])
    image = np.full((96, 128, 3), 128, dtype=np.uint8)
    rows = []
    for case in corpus["cases"]:
        provider = ScriptedProvider([{"content": raw_answer(corpus["base"], case),
                                      "finish": case.get("finish", "stop")}] * profile.attempts, model=profile.model)
        result = await analyze_model(profile=profile, threshold=float(corpus["threshold"]), provider=provider,
                                     provider_id="scripted", model=profile.model,
                                     references=[image] * profile.references, current=image,
                                     asset="adversarial corpus asset")
        got = result.verdict if result.verdict != "refused" else f"refused:{result.reasons[0]}"
        rows.append({"id": case["id"], "category": case["category"], "expect": case["expect"], "got": got,
                     "passed": got == case["expect"],
                     "escape": case["expect"].startswith("refused:") and result.verdict != "refused"})
    return {"format": "drone.vision-adversarial-report/v1", "profile_sha256": profile_sha,
            "threshold": corpus["threshold"], "cases": len(rows), "passed": sum(r["passed"] for r in rows),
            "escapes": sum(r["escape"] for r in rows),
            "per_category": dict(sorted(Counter(r["category"] for r in rows).items())),
            "status": "passed" if all(r["passed"] for r in rows) else "failed",
            "failures": [r for r in rows if not r["passed"]]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    args = parser.parse_args()
    report = asyncio.run(run_corpus(args.root))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
