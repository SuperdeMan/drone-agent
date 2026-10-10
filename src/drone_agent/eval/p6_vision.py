"""S0 capture effects for P6 (D078): glare and shadow over the target, blur, and capture drift; test doubles only.

The logical camera is the P4 one (a 160x120 frame, the 2 m marker seen from 4 m spans 40 px, so 20 px per metre at
the marker). `render` draws the registered marker where a capture from a drifted position sees it, with the P4 damage
band, a glare disc (white) or a shadow disc (near black) over its centre, or a box blur of the whole frame, plus the
P4 nonce so two captures never share bytes. `P6Appearance` keeps these effects per (robot, asset) beside the P4
appearance, and per robot the drift that the logical aircraft also applies to its capture pose; the heading is
fixed at zero (image up is +x, image left is +y), which the quality layer never relies on.

P6 的 S0 采集效果（D078）：目标上的反光与阴影、模糊与拍摄位置偏差；只是测试替身。

逻辑相机与 P4 相同（160x120 画面，从 4 m 看 2 m 的标记占 40 像素，即标记处每米 20 像素）。`render` 把登记标记画在偏差
位置的采集所看到的位置，可带 P4 损伤带、中心处的反光圆片（白色）或阴影圆片（近黑），或整幅画面的方框模糊，并加上 P4
的随机标记使两次采集的字节永不相同。`P6Appearance` 在 P4 外观之外按（机器人，资产）保存这些效果，并按机器人保存逻辑
飞行器同样施加在拍摄位姿上的偏差；航向固定为零（画面上方为 +x、左方为 +y），质量层从不依赖它。
"""

from __future__ import annotations

import hashlib

import numpy as np

from drone_agent.eval.scripted_vision import AppearanceWorld

WIDTH, HEIGHT, MARKER_PX, PX_PER_M = 160, 120, 40, 20
COLOURS = {"red": (230, 20, 20), "green": (20, 230, 20), "blue": (20, 20, 230)}
# The P4 damage band: rows 16-23 of the 40-row marker. / P4 损伤带：40 行标记中的第 16-23 行。
DAMAGE_ROWS = (16, 24)
OVERLAYS = {"glare": (17, (255, 255, 255)), "shadow": (19, (10, 10, 10))}


def shift_px(offset_m: tuple[float, float]) -> tuple[int, int]:
    """Where the marker moves in the frame when the capture position is off by `offset_m` (x, y).

    拍摄位置偏差 `offset_m`（x, y）时标记在画面中的位移。
    """
    ox, oy = offset_m
    return round(PX_PER_M * oy), round(PX_PER_M * ox)


def render(signature: str | None, *, damaged: bool = False, overlay: str | None = None, blur: int = 0,
           offset_m: tuple[float, float] = (0.0, 0.0), nonce: int | None = None) -> bytes:
    """One 160x120 logical capture of the marker with the given effects. / 带给定效果的一帧 160x120 逻辑采集。"""
    pixels = np.full((HEIGHT, WIDTH, 3), 128, dtype=np.uint8)
    du, dv = shift_px(offset_m)
    top, left = (HEIGHT - MARKER_PX) // 2 + dv, (WIDTH - MARKER_PX) // 2 + du
    if signature in COLOURS:
        rows = slice(max(0, top), max(0, min(HEIGHT, top + MARKER_PX)))
        columns = slice(max(0, left), max(0, min(WIDTH, left + MARKER_PX)))
        pixels[rows, columns] = COLOURS[signature]
        if damaged:
            band = slice(max(0, top + DAMAGE_ROWS[0]), max(0, min(HEIGHT, top + DAMAGE_ROWS[1])))
            pixels[band, columns] = 0
    if overlay is not None:
        radius, colour = OVERLAYS[overlay]
        cy, cx = top + MARKER_PX / 2, left + MARKER_PX / 2
        y, x = np.mgrid[0:HEIGHT, 0:WIDTH]
        pixels[np.hypot(x + 0.5 - cx, y + 0.5 - cy) <= radius] = colour
    if blur > 1:
        padded = np.pad(pixels.astype(np.float32), ((blur // 2, blur // 2), (blur // 2, blur // 2), (0, 0)),
                        mode="edge")
        summed = np.zeros((HEIGHT, WIDTH, 3), dtype=np.float32)
        for row in range(blur):
            for column in range(blur):
                summed += padded[row:row + HEIGHT, column:column + WIDTH]
        pixels = np.round(summed / (blur * blur)).astype(np.uint8)
    if nonce is not None:
        for index in range(4):
            level = 100 + (nonce >> (6 * index)) % 56
            pixels[0, index] = (level, level, level)
    return pixels.tobytes()


class P6Appearance(AppearanceWorld):
    """The P4 appearance world plus target overlays, blur and capture drift. / P4 外观世界加目标遮挡、模糊与拍摄偏差。"""

    def __init__(self, signatures: dict[str, str]):
        super().__init__(signatures)
        self.overlays: dict[tuple[str, str], str] = {}
        self.blurred: set[tuple[str, str]] = set()
        self.drift: dict[str, tuple[float, float]] = {}

    def effect(self, robot_id: str, asset_id: str, *, overlay: str | None = None, blur: bool = False) -> None:
        """Set what the next captures of the asset show; no arguments clears them. / 设置该资产后续采集所呈现的效果。"""
        key = (robot_id, asset_id)
        if overlay is None:
            self.overlays.pop(key, None)
        else:
            self.overlays[key] = overlay
        (self.blurred.add if blur else self.blurred.discard)(key)

    def camera(self, robot_id: str):
        def capture(asset_id: str) -> bytes:
            self.captures += 1
            key = (robot_id, asset_id)
            if robot_id in self.replays:
                raw, state = self.replays.pop(robot_id), "replayed"
            else:
                damaged = key in self.damaged
                raw = render(self.signatures.get(asset_id), damaged=damaged, overlay=self.overlays.get(key),
                             blur=9 if key in self.blurred else 0, offset_m=self.drift.get(robot_id, (0.0, 0.0)),
                             nonce=self.captures)
                state = "damaged" if damaged else "normal"
            self.history.append({"robot_id": robot_id, "asset_id": asset_id, "state": state, "capture": self.captures,
                                 "overlay": self.overlays.get(key), "blurred": key in self.blurred,
                                 "drift_m": list(self.drift.get(robot_id, (0.0, 0.0))),
                                 "sha256": hashlib.sha256(raw).hexdigest()})
            return raw
        return capture
