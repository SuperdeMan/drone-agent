"""Deterministic colour-signature detection and ground projection (WP-M3-07, D041); pure numpy, no ROS imports.

A pixel belongs to a signature when that channel exceeds 70 and 1.5 times both others (the M1/M2 evidence rule).
The blob centroid of the downward camera is cast onto the flat ground through the PX4 attitude and position; the
covariance combines estimator error, attitude error and pixel quantization at the measured height. No detection
without a covariance leaves this module.

确定性颜色特征检测与地面投影（WP-M3-07，D041）；纯 numpy，不导入 ROS。

某通道大于 70 且大于另两通道 1.5 倍时像素属于该特征（沿用 M1/M2 证据规则）。下视相机的色块质心经 PX4 姿态与位置
投射到平坦地面；协方差按实测高度合成估计器误差、姿态误差与像素量化。不带协方差的检测不会离开本模块。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from da_common.px4 import frd_to_ned

CHANNEL = {"red": 0, "green": 1, "blue": 2}


@dataclass(frozen=True)
class Detection:
    signature: str
    fraction: float
    position_enu: tuple[float, float, float]
    sigma_m: float
    pixel: tuple[float, float]


def signature_masks(rgb: np.ndarray) -> dict[str, np.ndarray]:
    image = rgb.astype(np.int32)
    masks = {}
    for name, index in CHANNEL.items():
        others = [image[..., i] for i in range(3) if i != index]
        channel = image[..., index]
        masks[name] = (channel > 70) & (channel * 2 > others[0] * 3) & (channel * 2 > others[1] * 3)
    return masks


def detect(rgb: np.ndarray, *, hfov: float, position_ned, q_wxyz, eph_m: float, min_fraction: float = 0.02,
           attitude_sigma_rad: float = math.radians(2.0)) -> list[Detection]:
    """Detections of each colour signature projected to the ground, in map ENU. / 各颜色特征投影到地面的检测（地图 ENU）。"""
    height, width, _ = rgb.shape
    fx = (width / 2) / math.tan(hfov / 2)
    cx, cy = (width - 1) / 2, (height - 1) / 2
    above = -float(position_ned[2])
    if above <= 0.5:
        return []
    results = []
    for name, mask in signature_masks(rgb).items():
        fraction = float(mask.mean())
        if fraction < min_fraction:
            continue
        rows, cols = np.nonzero(mask)
        u, v = float(cols.mean()), float(rows.mean())
        # Downward camera: image right is body right, image down is body backward. / 下视相机：图像右为机体右，图像下为机体后。
        ray_ned = frd_to_ned(q_wxyz, (-(v - cy) / fx, (u - cx) / fx, 1.0))
        if ray_ned[2] <= 1e-3:
            continue
        scale = above / ray_ned[2]
        north, east = position_ned[0] + scale * ray_ned[0], position_ned[1] + scale * ray_ned[1]
        sigma = math.sqrt(eph_m ** 2 + (above * math.tan(attitude_sigma_rad)) ** 2 + (above * 2.0 / fx) ** 2)
        results.append(Detection(name, fraction, (east, north, 0.0), sigma, (u, v)))
    return results


def match_asset(detection: Detection, assets: dict, max_distance_m: float = 3.0) -> str | None:
    """The registered asset with the same signature within reach, if any. / 距离内同特征的登记资产（若有）。"""
    best, best_distance = None, max_distance_m
    for asset_id, asset in assets.items():
        if asset.get("visual_signature") != detection.signature:
            continue
        distance = math.dist(asset["position"][:2], detection.position_enu[:2])
        if distance <= best_distance:
            best, best_distance = asset_id, distance
    return best
