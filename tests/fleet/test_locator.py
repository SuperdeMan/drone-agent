"""X1 anomaly locator (D088): the centroid of a declared colour, no detection below the pixel floor.

X1 异常定位（D088）：声明颜色的质心；低于像素下限时判为未检测到。
"""

from __future__ import annotations

import numpy as np
import pytest

from drone_agent.fleet.locator import COLOURS, MIN_PIXELS, locate


def frame(*blocks) -> np.ndarray:
    image = np.full((120, 160, 3), 90, dtype=np.uint8)
    for colour, (row, col, size) in blocks:
        image[row:row + size, col:col + size] = COLOURS[colour]
    return image


def test_the_centroid_is_the_block_centre_in_pixel_coordinates():
    found = locate(frame(("orange", (10, 40, 10))), "orange")
    assert (found.u, found.v, found.pixels) == (45.0, 15.0, 100)


def test_other_colours_and_noise_are_ignored():
    image = frame(("orange", (60, 100, 8)), ("magenta", (10, 10, 8)))
    image[0, 0] = COLOURS["orange"]  # one stray pixel / 一个杂散像素
    found = locate(image, "magenta")
    assert (found.u, found.v) == (14.0, 14.0)


def test_too_few_pixels_is_no_detection():
    small = frame(("orange", (10, 10, 3)))
    assert 9 < MIN_PIXELS and locate(small, "orange") is None
    assert locate(frame(), "orange") is None


def test_an_unknown_colour_or_a_non_rgb_image_is_refused():
    with pytest.raises(ValueError):
        locate(frame(), "teal")
    with pytest.raises(ValueError):
        locate(np.zeros((10, 10), dtype=np.uint8), "orange")
