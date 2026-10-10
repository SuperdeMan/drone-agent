"""S0 appearance world and scripted vision double for P4 (D063 §12); labelled test doubles, never model behaviour.

`AppearanceWorld` is the simulated world's view of registered assets: each asset is `normal` or `damaged` (a dark band
across its colour marker), every capture carries a small grey nonce so two captures never share bytes, and the
harness can make the camera replay an earlier frame byte for byte. `ScriptedVisionProvider` answers the model
analyzer's exact JSON by looking at the current image the way a scripted double may: it finds the marker and counts
dark pixels inside it. Fault modes make it time out, return prose, refuse, or smuggle decision fields that the
schema must reject. Its source is `scripted`; nothing it says counts as a model result.

P4 的 S0 外观世界与脚本视觉替身（D063 §12）；带标注的测试替身，从不当作模型行为。

`AppearanceWorld` 是模拟世界对登记资产的视图：每个资产为 `normal` 或 `damaged`（颜色标记上的一条暗带），每次采集带一个
小的灰度随机标记，使两次采集的字节永不相同，编排还可以让相机逐字节重放先前的一帧。`ScriptedVisionProvider` 以脚本替身
可用的方式回答模型分析器要求的确切 JSON：找到标记并统计其中的暗像素。故障模式可使它超时、返回散文、拒答，或夹带 schema
必须拒绝的决定字段。其来源为 `scripted`；它说的任何内容都不计为模型结果。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json

import numpy as np
from PIL import Image

from drone_agent.eval.logical_flight import image
from drone_agent.providers.replay import ScriptedProvider

# The marker of a 160x120 logical frame spans rows 40-79 and columns 60-99; damage darkens rows 56-63 of it.
# 160x120 逻辑帧的标记占第 40-79 行、第 60-99 列；损伤使其中第 56-63 行变暗。
DAMAGE_ROWS = range(56, 64)
MARKER_COLUMNS = range(60, 100)


def frame(signature: str | None, *, damaged: bool, nonce: int | None) -> bytes:
    """A 160x120 logical frame of one appearance; the nonce greys four corner pixels. / 某外观的一帧逻辑影像。"""
    pixels = bytearray(image(signature))
    if damaged:
        for row in DAMAGE_ROWS:
            for column in MARKER_COLUMNS:
                offset = (row * 160 + column) * 3
                pixels[offset:offset + 3] = b"\x00\x00\x00"
    if nonce is not None:
        for index in range(4):
            level = 100 + (nonce >> (6 * index)) % 56
            pixels[index * 3:index * 3 + 3] = bytes((level, level, level))
    return bytes(pixels)


class AppearanceWorld:
    """The harness camera of the S0 world: appearance per (robot, asset), a capture counter and replay.

    S0 世界的编排相机：按（机器人，资产）的外观、采集计数与重放。
    """

    def __init__(self, signatures: dict[str, str]):
        self.signatures = dict(signatures)
        self.damaged: set[tuple[str, str]] = set()
        self.captures = 0
        self.replays: dict[str, bytes] = {}
        self.history: list[dict] = []

    def set(self, robot_id: str, asset_id: str, state: str) -> None:
        (self.damaged.add if state == "damaged" else self.damaged.discard)((robot_id, asset_id))

    def camera(self, robot_id: str):
        def capture(asset_id: str) -> bytes:
            self.captures += 1
            if robot_id in self.replays:
                raw = self.replays.pop(robot_id)
                state = "replayed"
            else:
                damaged = (robot_id, asset_id) in self.damaged
                raw = frame(self.signatures.get(asset_id), damaged=damaged, nonce=self.captures)
                state = "damaged" if damaged else "normal"
            self.history.append({"robot_id": robot_id, "asset_id": asset_id, "state": state,
                                 "capture": self.captures})
            return raw
        return capture


def _current_image(messages) -> np.ndarray | None:
    """The last image of the last user message, decoded. / 解码最后一条用户消息中的最后一张图。"""
    user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    parts = user.get("content") if isinstance(user, dict) and isinstance(user.get("content"), list) else []
    urls = [p["image_url"]["url"] for p in parts if isinstance(p, dict) and p.get("type") == "image_url"]
    if not urls or "," not in urls[-1]:
        return None
    data = base64.b64decode(urls[-1].split(",", 1)[1])
    with Image.open(io.BytesIO(data)) as picture:
        return np.asarray(picture.convert("RGB"), dtype=np.uint8)


def judge(pixels: np.ndarray) -> dict:
    """What the scripted double 'sees': a saturated marker, and dark pixels inside it. / 替身「看到」的内容。"""
    red, green, blue = (pixels[..., i].astype(np.int32) for i in range(3))
    marker = ((red > 70) & (red > green * 1.5) & (red > blue * 1.5)) | \
        ((green > 70) & (green > red * 1.5) & (green > blue * 1.5)) | \
        ((blue > 70) & (blue > red * 1.5) & (blue > green * 1.5))
    rows, columns = np.nonzero(marker)
    if rows.size == 0:
        return {"image_usable": True, "target_matches_reference": False, "anomaly_score": 0.0, "defect_type": "none",
                "description": "scripted: no registered marker in view", "unusable_reason": "none"}
    box = pixels[rows.min():rows.max() + 1, columns.min():columns.max() + 1]
    dark = float((box.max(axis=2) < 30).mean())
    if dark > 0.05:
        return {"image_usable": True, "target_matches_reference": True, "anomaly_score": 0.92, "defect_type": "damage",
                "description": "scripted: dark band across the marker", "unusable_reason": "none"}
    return {"image_usable": True, "target_matches_reference": True, "anomaly_score": 0.05, "defect_type": "none",
            "description": "scripted: marker matches the reference", "unusable_reason": "none"}


class ScriptedVisionProvider(ScriptedProvider):
    """A labelled scripted vision double with harness fault modes. / 带编排故障模式、带标注的脚本视觉替身。"""

    def __init__(self, model: str = "scripted-vision"):
        super().__init__([], model=model)
        self.mode: str | None = None
        self.delay_s = 0.0

    async def complete(self, messages, model, temperature, max_tokens, thinking=None, timeout_s=None,
                       reasoning_effort=None):
        self.calls.append({"messages": len(messages)})
        if self.mode == "timeout":
            await asyncio.sleep((timeout_s or 5) + 2)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.mode == "error":
            raise RuntimeError("scripted provider failure")
        if self.mode == "refusal":
            return "", self.model, "refusal", (120, 0)
        if self.mode == "malformed":
            return "The marker looks fine to me.", self.model, "stop", (120, 12)
        pixels = _current_image(messages)
        answer = judge(pixels) if pixels is not None else {
            "image_usable": False, "target_matches_reference": False, "anomaly_score": 0.0, "defect_type": "none",
            "description": "scripted: no image", "unusable_reason": "other"}
        if self.mode == "inject":
            # Decision fields a model must never be able to set; the schema rejects the whole answer.
            # 模型永远不能设置的决定字段；schema 会拒绝整个回答。
            answer = {**answer, "decision": "confirmed", "approve": True, "close_order": True}
        if self.mode in ("mismatch", "occluded"):
            # P6 (D078): a well-formed answer that the capture shows another object, or that the target is hidden.
            # P6（D078）：格式合规的回答：采集拍到的是别的对象，或目标被遮挡。
            answer = {**answer, "target_matches_reference": self.mode != "mismatch",
                      "image_usable": self.mode != "occluded", "anomaly_score": 0.0, "defect_type": "none",
                      "unusable_reason": "occluded" if self.mode == "occluded" else "none",
                      "description": f"scripted: {self.mode}"}
        return json.dumps(answer), self.model, "stop", (180, 40)


class VisionConfig:
    """The minimal provider configuration the business engine reads. / 业务引擎读取的最小 provider 配置。"""

    def __init__(self, provider_id: str = "scripted", model: str = "scripted-vision", endpoint_host: str = ""):
        self.provider_id, self.model, self.endpoint_host = provider_id, model, endpoint_host
