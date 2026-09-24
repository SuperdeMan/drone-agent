"""Pure logic of the event detector: CLIP tokenizer, preprocessing, class scoring and the trigger rule (D044).

The real vocabulary is checked at image build time against CLIP's reference ids; here a miniature vocabulary pins the
merge algorithm, and the pins in the prompt set are compared with the image build arguments.

事件检测的纯逻辑：CLIP 分词、预处理、类别打分与触发规则（D044）。真实词表在镜像构建时对照 CLIP 参考序列检查；这里用
微型词表钉住合并算法，并比对提示集中的固定值与镜像构建参数。
"""

import re
from pathlib import Path

import numpy as np
import pytest
import yaml
from da_edge_inference.clip import (
    EOS,
    MEAN,
    STD,
    Tokenizer,
    bytes_to_unicode,
    class_embeddings,
    classify,
    decide,
    preprocess,
)

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = yaml.safe_load((ROOT / "configs/perception/event_prompts_v1.yaml").read_text(encoding="utf-8"))


def mini_tokenizer():
    vocab = {"<|startoftext|>": 0, EOS: 1, "c": 2, "a": 3, "t": 4, "t</w>": 5, "a</w>": 6, "ca": 7, "cat</w>": 8,
             "s</w>": 9, "cats</w>": 10, "s": 11, "cat": 12, "-</w>": 13, "-": 14}
    merges = "#version: 0.2\nc a\nt </w>\nca t</w>\nca t\ncat s</w>\n"
    return Tokenizer.from_text(vocab, merges)


def test_bpe_merges_by_rank_and_wraps_the_sequence():
    tokenizer = mini_tokenizer()
    assert tokenizer.bpe("cat") == ["cat</w>"]
    assert tokenizer.bpe("cats") == ["cats</w>"]
    assert tokenizer.bpe("a") == ["a</w>"]
    assert tokenizer.encode("cat  cats") == [0, 8, 10, 1]
    # Punctuation is its own pre-token, as in CLIP's pattern. / 标点自成预切分单元，与 CLIP 的正则一致。
    assert tokenizer.encode("cat-cat") == [0, 8, 13, 8, 1]


def test_only_lowercase_ascii_prompts_within_the_context_are_accepted():
    tokenizer = mini_tokenizer()
    for text in ("Cat", "caté", "cat​"):
        with pytest.raises(ValueError, match="lowercase ASCII"):
            tokenizer.encode(text)
    with pytest.raises(ValueError, match="77-token"):
        tokenizer.encode(" ".join(["cat"] * 80))


def test_the_byte_table_is_a_bijection_over_all_bytes():
    table = bytes_to_unicode()
    assert sorted(table) == list(range(256)) and len(set(table.values())) == 256


def test_preprocessing_follows_the_clip_input_contract():
    grey = np.full((120, 160, 3), 218, dtype=np.uint8)
    pixels = preprocess(grey)
    assert pixels.shape == (1, 3, 224, 224) and pixels.dtype == np.float32
    expected = (218 / 255 - MEAN) / STD
    assert np.allclose(pixels[0, :, 112, 112], expected, atol=1e-5)
    # A centred square stays centred after the shortest-edge resize and centre crop. / 最短边缩放与中心裁剪后仍居中。
    marked = grey.copy()
    marked[50:70, 70:90] = (0, 255, 0)
    green = preprocess(marked)[0, 1]
    assert green[112, 112] > green[5, 5] and green[112, 112] > green[5, 218]
    with pytest.raises(ValueError):
        preprocess(np.zeros((120, 160), dtype=np.uint8))


def test_class_scores_ensemble_prompts_and_sum_to_one():
    rng = np.random.default_rng(3)
    basis = np.linalg.qr(rng.normal(size=(512, 3)))[0].T
    table = {"alpha": [basis[0] + 0.05 * basis[1], basis[0] - 0.05 * basis[1]], "beta": [basis[1]], "gamma": [basis[2]]}
    labels, classes = class_embeddings(table)
    probabilities = classify(basis[0] * 7.0, labels, classes)
    assert labels == ["alpha", "beta", "gamma"] and pytest.approx(sum(probabilities.values())) == 1.0
    assert probabilities["alpha"] > 0.99
    with pytest.raises(ValueError):
        classify(np.zeros(512), labels, classes)


def test_only_a_confident_trigger_class_raises_the_replan_trigger():
    classes = PROMPTS["classes"]
    threshold = PROMPTS["runtime"]["trigger_probability"]
    assert decide({"person": 0.9, "empty_ground": 0.1}, classes, threshold) == ("person", 0.9, True)
    assert decide({"person": 0.5, "empty_ground": 0.4, "marker_red": 0.1}, classes, threshold)[2] is False
    assert decide({"marker_green": 0.99, "person": 0.01}, classes, threshold)[2] is False
    # Every trigger class is also marked relevant, and the markers never trigger. / 触发类都标为相关，标记类从不触发。
    assert all(spec["relevant"] for spec in classes.values() if spec["replan_trigger"])
    assert not any(classes[name]["replan_trigger"] for name in ("marker_green", "marker_red", "marker_blue"))


def test_the_prompt_set_pins_match_the_image_build_arguments():
    dockerfile = (ROOT / "sim/m3.Dockerfile").read_text(encoding="utf-8")
    args = dict(re.findall(r"^ARG (\w+)=(\S+)$", dockerfile, re.MULTILINE))
    model = PROMPTS["model"]
    assert args["CLIP_REVISION"] == model["revision"]
    assert args["CLIP_VISION_SHA256"] == model["vision"]["sha256"]
    assert args["CLIP_TEXT_SHA256"] == model["text"]["sha256"]
    assert args["CLIP_VOCAB_SHA256"] == model["vocab"]["sha256"]
    assert args["CLIP_MERGES_SHA256"] == model["merges"]["sha256"]
    for spec in PROMPTS["classes"].values():
        for prompt in spec["prompts"]:
            # Every prompt passes the ASCII rule before the real vocabulary is consulted. / 每条提示都满足 ASCII 规则。
            assert re.fullmatch(r"[a-z0-9 ,.'-]+", prompt), prompt
