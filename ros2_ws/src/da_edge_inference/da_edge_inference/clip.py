"""CLIP zero-shot scene classification logic (WP-M3-15, D041, D044); numpy only, no ROS or model runtime imports.

The CLIP byte-level BPE tokenizer is restricted to ASCII prompts, where the reference pattern's Unicode letter and
number classes reduce to [a-z] and [0-9]; the image path follows CLIPImageProcessor (shortest edge to 224, centre crop,
CLIP mean and std) with bilinear instead of bicubic resampling. Class scores average the normalized prompt embeddings
of each class (prompt ensembling) and apply CLIP's logit scale of 100 before the softmax. Nothing here decides
anything: the node turns the scores into candidate facts.

CLIP 零样本场景分类逻辑（WP-M3-15，D041，D044）；只用 numpy，不导入 ROS 或模型运行时。

CLIP 字节级 BPE 分词只接受 ASCII 提示，此时参考正则中的 Unicode 字母与数字类退化为 [a-z] 与 [0-9]；图像路径遵循
CLIPImageProcessor（最短边缩放到 224、中心裁剪、CLIP 均值与标准差），重采样用双线性代替双三次。类别分数对每类归一化
后的提示嵌入取平均（提示集成），乘以 CLIP 的 logit 尺度 100 后做 softmax。这里不做任何决定：节点把分数变成候选事实。
"""

from __future__ import annotations

import re

import numpy as np

BOS, EOS = "<|startoftext|>", "<|endoftext|>"
IMAGE_SIZE = 224
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
LOGIT_SCALE = 100.0
# The reference pattern on ASCII input. / 参考正则在 ASCII 输入上的形式。
PATTERN = re.compile(r"'s|'t|'re|'ve|'m|'ll|'d|[a-z]+|[0-9]|[^\sa-z0-9]+")
PROMPT = re.compile(r"^[a-z0-9 ,.'-]+$")


def bytes_to_unicode() -> dict[int, str]:
    """GPT-2 / CLIP reversible byte-to-character table. / GPT-2 / CLIP 的可逆字节到字符映射。"""
    printable = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(
        range(ord("®"), ord("ÿ") + 1))
    codes = printable[:]
    extra = 0
    for byte in range(256):
        if byte not in printable:
            printable.append(byte)
            codes.append(256 + extra)
            extra += 1
    return dict(zip(printable, (chr(code) for code in codes), strict=True))


class Tokenizer:
    """CLIP BPE over a vocab mapping and ranked merges (the files of the pinned model revision).

    基于词表映射与有序合并规则（固定模型版本的文件）的 CLIP BPE。
    """

    def __init__(self, vocab: dict[str, int], merges: list[tuple[str, str]]):
        self.vocab, self.ranks = vocab, {pair: rank for rank, pair in enumerate(merges)}
        self.byte_encoder = bytes_to_unicode()
        self.cache: dict[str, list[str]] = {}

    @classmethod
    def from_text(cls, vocab: dict[str, int], merges_text: str) -> Tokenizer:
        lines = [line for line in merges_text.splitlines() if line and not line.startswith("#version")]
        return cls(vocab, [tuple(line.split()) for line in lines])

    def bpe(self, token: str) -> list[str]:
        if token in self.cache:
            return self.cache[token]
        word = [*token[:-1], token[-1] + "</w>"]
        while len(word) > 1:
            pairs = {(a, b) for a, b in zip(word, word[1:], strict=False)}
            best = min(pairs, key=lambda pair: self.ranks.get(pair, len(self.ranks) + 1))
            if best not in self.ranks:
                break
            merged, index = [], 0
            while index < len(word):
                if index < len(word) - 1 and (word[index], word[index + 1]) == best:
                    merged.append(word[index] + word[index + 1])
                    index += 2
                else:
                    merged.append(word[index])
                    index += 1
            word = merged
        self.cache[token] = word
        return word

    def encode(self, text: str) -> list[int]:
        """Token ids with start and end markers; only lowercase ASCII prompts are accepted.

        带起止标记的 token 序列；只接受小写 ASCII 提示。
        """
        cleaned = " ".join(text.split())
        if not PROMPT.match(cleaned):
            raise ValueError(f"prompts must be lowercase ASCII: {text!r}")
        ids = [self.vocab[BOS]]
        for token in PATTERN.findall(cleaned):
            mapped = "".join(self.byte_encoder[byte] for byte in token.encode("ascii"))
            ids.extend(self.vocab[piece] for piece in self.bpe(mapped))
        ids.append(self.vocab[EOS])
        if len(ids) > 77:
            raise ValueError("prompt longer than CLIP's 77-token context")
        return ids


def resize_bilinear(image: np.ndarray, height: int, width: int) -> np.ndarray:
    """Half-pixel-centred bilinear resize of an HxWxC float array. / 半像素中心对齐的双线性缩放。"""
    source_h, source_w = image.shape[:2]
    ys = np.clip((np.arange(height) + 0.5) * source_h / height - 0.5, 0, source_h - 1)
    xs = np.clip((np.arange(width) + 0.5) * source_w / width - 0.5, 0, source_w - 1)
    y0, x0 = np.floor(ys).astype(int), np.floor(xs).astype(int)
    y1, x1 = np.minimum(y0 + 1, source_h - 1), np.minimum(x0 + 1, source_w - 1)
    wy, wx = (ys - y0)[:, None, None], (xs - x0)[None, :, None]
    top = image[y0][:, x0] * (1 - wx) + image[y0][:, x1] * wx
    bottom = image[y1][:, x0] * (1 - wx) + image[y1][:, x1] * wx
    return top * (1 - wy) + bottom * wy


def preprocess(rgb: np.ndarray) -> np.ndarray:
    """HxWx3 uint8 RGB -> 1x3x224x224 float32 CLIP input. / HxWx3 uint8 RGB -> 1x3x224x224 float32 CLIP 输入。"""
    if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
        raise ValueError("expected an HxWx3 uint8 RGB image")
    height, width = rgb.shape[:2]
    scale = IMAGE_SIZE / min(height, width)
    new_h, new_w = (IMAGE_SIZE, int(width * scale)) if height <= width else (int(height * scale), IMAGE_SIZE)
    resized = resize_bilinear(rgb.astype(np.float32) / 255.0, new_h, new_w)
    top, left = (new_h - IMAGE_SIZE) // 2, (new_w - IMAGE_SIZE) // 2
    crop = resized[top:top + IMAGE_SIZE, left:left + IMAGE_SIZE]
    normalized = (crop - MEAN) / STD
    return np.ascontiguousarray(normalized.transpose(2, 0, 1)[None], dtype=np.float32)


def normalize(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    if not np.all(np.isfinite(norms)) or np.any(norms <= 0):
        raise ValueError("embeddings must be finite and non-zero")
    return vectors / norms


def class_embeddings(prompt_embeddings: dict[str, list[list[float]]]) -> tuple[list[str], np.ndarray]:
    """Average the normalized prompt embeddings of each class and renormalize. / 每类提示嵌入归一化后取平均再归一化。"""
    labels = sorted(prompt_embeddings)
    rows = [normalize(np.mean(normalize(np.asarray(prompt_embeddings[label])), axis=0)) for label in labels]
    return labels, np.stack(rows)


def classify(image_embedding: np.ndarray, labels: list[str], classes: np.ndarray) -> dict[str, float]:
    """Softmax over the classes of CLIP-scaled cosine similarities. / 对 CLIP 缩放余弦相似度做类别 softmax。"""
    image = normalize(np.asarray(image_embedding, dtype=np.float32).reshape(-1))
    logits = LOGIT_SCALE * (classes @ image)
    logits = logits - logits.max()
    weights = np.exp(logits)
    probabilities = weights / weights.sum()
    return {label: float(p) for label, p in zip(labels, probabilities, strict=True)}


def decide(probabilities: dict[str, float], classes: dict[str, dict], trigger_probability: float) -> tuple[str, float, bool]:
    """Winning class, its probability, and whether it raises a replan trigger (D044).

    Only a class that declares `replan_trigger` and wins with at least `trigger_probability` raises it; the trigger is a
    request for the bounded replan, never an action.

    胜出类别、其概率，以及是否发起重规划触发（D044）。只有声明了 `replan_trigger` 且以不低于 `trigger_probability`
    的概率胜出的类别才会发起；触发只是对有界重规划的请求，从来不是动作。
    """
    label = max(probabilities, key=probabilities.get)
    probability = probabilities[label]
    return label, probability, bool(classes[label]["replan_trigger"] and probability >= trigger_probability)
