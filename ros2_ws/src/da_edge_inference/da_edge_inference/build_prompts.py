"""Build-time prompt embeddings for the onboard event detector (WP-M3-15, D041, D044).

Runs once in a throwaway image stage: verifies the pinned CLIP text encoder, vocabulary and merges by SHA-256, checks
the tokenizer against CLIP's known token ids, encodes every prompt of the versioned prompt set and writes the class
table with its provenance. The text encoder never enters the aircraft image; at run time only the vision encoder runs.

机载事件检测的构建期提示嵌入（WP-M3-15，D041，D044）。

在一次性镜像阶段运行一次：按 SHA-256 核对固定的 CLIP 文本编码器、词表与合并规则，用 CLIP 已知的 token 序列自检
分词器，编码版本化提示集中的每条提示，并写出带来源信息的类别表。文本编码器不进入机载镜像；运行时只跑视觉编码器。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import yaml

from da_edge_inference.clip import Tokenizer

# CLIP's own ids for a reference sentence; a mismatch means the tokenizer or its files are wrong.
# CLIP 对参考句子的 token 序列；不一致说明分词器或其文件有误。
REFERENCE = ("a photo of a cat", [49406, 320, 1125, 539, 320, 2368, 49407])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verified(directory: Path, pin: dict) -> Path:
    path = directory / Path(pin["file"]).name
    if sha256(path) != pin["sha256"]:
        raise ValueError(f"{pin['file']} does not match its pinned SHA-256")
    return path


def build(config: dict, model_dir: Path) -> dict:
    import onnxruntime as ort

    model = config["model"]
    vocab = json.loads(verified(model_dir, model["vocab"]).read_text(encoding="utf-8"))
    tokenizer = Tokenizer.from_text(vocab, verified(model_dir, model["merges"]).read_text(encoding="utf-8"))
    if tokenizer.encode(REFERENCE[0]) != REFERENCE[1]:
        raise ValueError("the CLIP tokenizer does not reproduce the reference token ids")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    session = ort.InferenceSession(str(verified(model_dir, model["text"])), options,
                                   providers=["CPUExecutionProvider"])
    names = [output.name for output in session.get_outputs()]
    classes = {}
    for label, spec in sorted(config["classes"].items()):
        rows = []
        for prompt in spec["prompts"]:
            ids = np.array([tokenizer.encode(prompt)], dtype=np.int64)
            feed = {item.name: ids if item.name == "input_ids" else np.ones_like(ids) for item in session.get_inputs()}
            embedding = dict(zip(names, session.run(None, feed), strict=True))["text_embeds"][0]
            if embedding.shape != (512,) or not np.all(np.isfinite(embedding)):
                raise ValueError(f"unexpected text embedding for {label!r}")
            rows.append([round(float(v), 7) for v in embedding])
        classes[label] = {"relevant": bool(spec["relevant"]), "replan_trigger": bool(spec["replan_trigger"]),
                          "prompts": list(spec["prompts"]), "embeddings": rows}
    return {
        "schema_version": config["schema_version"],
        "prompt_set": config["prompt_set"],
        "model": model,
        "runtime": config["runtime"],
        "classes": classes,
        "built_with": {"onnxruntime": ort.__version__},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    table = build(yaml.safe_load(args.config.read_text(encoding="utf-8")), args.model_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(table), encoding="utf-8")
    print(json.dumps({"prompt_set": table["prompt_set"], "classes": sorted(table["classes"])}))


if __name__ == "__main__":
    main()
