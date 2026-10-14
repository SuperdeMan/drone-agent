"""X1 anomaly locator (D088): the image centroid of an anomaly of a known colour, by deterministic code.

X1 measures the handoff and the spatial alignment, not recognition, so the anomaly objects of its worlds are blocks
of a declared colour and this locator is the only thing that reads them: the pixels within a tolerance of the colour
form the anomaly, their centroid is the annotation the alignment projects, and too few pixels is no detection rather
than a guess. The aerial annotation and the ground relocalization both use it, so a recheck compares like with like.
It never decides a business verdict: a located block is an observation, and confirming a defect stays with a person.

X1 异常定位（D088）：用确定性代码求已知颜色异常物的图像质心。

X1 测的是交接与空间对齐，不是识别，因此其世界里的异常物是声明颜色的方块，读取它们的只有本定位器：与该颜色相差在容差内
的像素构成异常物，其质心就是空间对齐所投影的标注；像素太少即判为未检测到，而不是猜测。空中标注与地面重新定位都用它，
因此复核是同一种测量的比较。它从不做业务判定：定位到方块只是一次观测，确认缺陷仍由人决定。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# The anomaly colours of the X1 worlds. / X1 世界中异常物的颜色。
COLOURS = {"orange": (255, 140, 0), "magenta": (230, 0, 230)}
TOLERANCE = 40  # max per-channel distance from the declared colour / 与声明颜色每个通道的最大差值
MIN_PIXELS = 12


@dataclass(frozen=True)
class Located:
    """Centroid in pixels (u right, v down from the top-left corner) and the pixel count. / 像素质心与像素数。"""

    u: float
    v: float
    pixels: int


def locate(image: np.ndarray, colour: str, *, tolerance: int = TOLERANCE, min_pixels: int = MIN_PIXELS
           ) -> Located | None:
    """The centroid of the pixels of `colour`; None when fewer than `min_pixels` match.

    `colour` 像素的质心；匹配像素少于 `min_pixels` 时为 None。
    """
    if colour not in COLOURS:
        raise ValueError(f"unknown anomaly colour {colour!r}")
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("expected an RGB image")
    target = np.array(COLOURS[colour], dtype=np.int16)
    mask = np.all(np.abs(image.astype(np.int16) - target) <= tolerance, axis=2)
    count = int(mask.sum())
    if count < min_pixels:
        return None
    rows, cols = np.nonzero(mask)
    # +0.5: a pixel's centre, so a block filling pixels 10..19 is centred at 15.0. / +0.5：像素中心。
    return Located(u=float(cols.mean() + 0.5), v=float(rows.mean() + 0.5), pixels=count)
