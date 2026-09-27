"""P4 S2 retrieval report (D065 §7): the pinned CLIP ViT-B/32 of the M3 event detector over the frozen VisA test split.

Text to image: each versioned query (encoded at image build time by the M3 prompt builder with the pinned text
encoder) ranks the test split's original normal and anomalous images by cosine similarity; relevance comes from the
frozen manifest (a VisA defect type, any anomaly, or a registered board), and the report gives AP@10 per query and
their mean. Image to reference: each of those images is assigned to the board whose registered reference images
(mean embedding) it is closest to, and the report gives top-1 device identification overall and per board. Images go
through the same preprocessing as the onboard detector. There is no threshold and nothing here decides anything; a
service retrieval entry is left to P5.

P4 S2 检索报告（D065 §7）：M3 事件检测固定的 CLIP ViT-B/32 在冻结 VisA 测试拆分上的表现。

文本到图像：每个版本化查询（由 M3 提示构建器在镜像构建期用固定文本编码器编码）按余弦相似度对测试拆分中的原始正常与
异常图像排序；相关性取自冻结清单（VisA 缺陷类型、任意异常或一块登记电路板），报告给出每个查询的 AP@10 及其均值。图像到
参考：把每张图像归到其登记参考图（平均嵌入）最接近的电路板，报告总体与分板的设备识别 top-1。图像预处理与机载检测相同。
不设门槛，这里也不做任何决定；服务内的检索入口留到 P5。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import yaml

CORPUS = ("normal", "anomalous")
K = 10


def relevant(sample: dict, match: dict) -> bool:
    if "asset" in match:
        return sample["asset"] == match["asset"]
    if "truth" in match:
        return sample["truth"] == match["truth"]
    return match["defect"] in sample["defects"].split(",")


def average_precision(ranked: list[bool], total: int, k: int = K) -> float | None:
    """AP@k with the usual normalisation by min(relevant, k); None without any relevant item.

    按 min(相关数, k) 归一化的 AP@k；没有相关项时为 None。
    """
    if total == 0:
        return None
    hits, score = 0, 0.0
    for rank, hit in enumerate(ranked[:k], start=1):
        if hit:
            hits += 1
            score += hits / rank
    return round(score / min(total, k), 4)


def normalized(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float32)
    return vectors / np.linalg.norm(vectors, axis=-1, keepdims=True)


def report(manifest: dict, config: dict, queries: dict, embed) -> dict:
    """The retrieval metrics given `embed(sample) -> vector`; pure apart from `embed`. / 给定 `embed` 的检索指标。"""
    corpus = sorted((s for s in manifest["samples"] if s["split"] == "test" and s["category"] in CORPUS),
                    key=lambda s: s["sample_id"])
    images = normalized(np.stack([embed(s) for s in corpus]))
    per_query = {}
    for label, spec in sorted(config["classes"].items()):
        prompts = normalized(np.asarray(queries["classes"][label]["embeddings"], dtype=np.float32))
        text = normalized(prompts.mean(axis=0))
        order = np.argsort(-(images @ text), kind="stable")
        flags = [relevant(corpus[i], spec["match"]) for i in order]
        per_query[label] = {"match": spec["match"], "relevant": int(sum(flags)),
                            "ap_at_10": average_precision(flags, int(sum(flags))),
                            "top10": [corpus[i]["sample_id"] for i in order[:K]]}
    scored = [q["ap_at_10"] for q in per_query.values() if q["ap_at_10"] is not None]
    boards = sorted(manifest["assets"])
    references = normalized(np.stack([normalized(np.stack([embed(s) for s in manifest["samples"]
                                                            if s["split"] == "reference" and s["asset"] == board]))
                                      .mean(axis=0) for board in boards]))
    predicted = [boards[int(np.argmax(references @ vector))] for vector in images]
    identification = {board: {"samples": sum(1 for s in corpus if s["asset"] == board),
                              "top1": round(sum(1 for s, p in zip(corpus, predicted, strict=True)
                                                if s["asset"] == board and p == board)
                                            / max(1, sum(1 for s in corpus if s["asset"] == board)), 4)}
                      for board in boards}
    correct = sum(1 for s, p in zip(corpus, predicted, strict=True) if s["asset"] == p)
    return {"format": "drone.s2-retrieval/v1", "corpus": len(corpus), "queries": len(per_query),
            "text_to_image": {"map_at_10": round(sum(scored) / len(scored), 4) if scored else None,
                              "per_query": per_query},
            "image_to_reference": {"top1": round(correct / len(corpus), 4), "per_board": identification},
            "threshold": None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True, help="the versioned query set")
    parser.add_argument("--queries", type=Path, required=True, help="its embeddings from the image build")
    parser.add_argument("--model", type=Path, required=True, help="the pinned CLIP vision encoder")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # The onboard detector's own preprocessing, shipped in the image's source tree. / 机载检测自身的预处理，随镜像源码提供。
    import onnxruntime as ort
    from da_edge_inference.clip import preprocess
    from PIL import Image

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    queries = json.loads(args.queries.read_text(encoding="utf-8"))
    digest = hashlib.sha256(args.model.read_bytes()).hexdigest()
    if digest != config["model"]["vision"]["sha256"] or queries["model"] != config["model"] or \
            queries["prompt_set"] != config["prompt_set"]:
        raise SystemExit("the vision model or the query embeddings do not match the pinned query set")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    session = ort.InferenceSession(str(args.model), options, providers=["CPUExecutionProvider"])
    name, outputs = session.get_inputs()[0].name, [o.name for o in session.get_outputs()]

    def embed(sample: dict) -> np.ndarray:
        raw = (args.data / sample["file"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != sample["sha256"]:
            raise SystemExit(f"{sample['sample_id']} differs from the manifest")
        with Image.open(args.data / sample["file"]) as picture:
            rgb = np.asarray(picture.convert("RGB"), dtype=np.uint8)
        result = dict(zip(outputs, session.run(None, {name: preprocess(rgb)}), strict=True))
        return result["image_embeds"][0]

    result = report(manifest, config, queries, embed)
    result.update({"manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
                   "query_set": config["prompt_set"], "query_set_sha256": hashlib.sha256(
                       args.config.read_bytes()).hexdigest(), "model": config["model"],
                   "onnxruntime": ort.__version__})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({k: result[k] for k in ("corpus", "queries", "text_to_image", "image_to_reference")}
                     | {"text_to_image": {"map_at_10": result["text_to_image"]["map_at_10"]}}))


if __name__ == "__main__":
    main()
