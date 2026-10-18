"""Reconstruct bounded native windows only from already verified local TIFF ranges.

仅从已验证的本地 TIFF 字节范围重建有界原生窗口。
"""
from __future__ import annotations

import hashlib
import io
import math
from collections import OrderedDict
from pathlib import Path

MAX_WINDOW_PIXELS = 60_000_000
MAX_READ_BYTES = 256 * 1024 * 1024
MAX_BLOCK_CACHE_BYTES = 64 * 1024 * 1024


class RangeFile(io.RawIOBase):
    """Expose only acquired byte blocks; missing bytes are errors, never zero-filled.

只暴露已取得的字节块；缺失字节报错，绝不补零。
"""

    def __init__(self, source, *, gdal_callback=False):
        super().__init__()
        self.source, self.position = source, 0
        self.gdal_callback = gdal_callback

    def readable(self):
        """Allow reads only. / 仅允许读取。"""
        return True

    def seekable(self):
        """Support local random reads. / 支持本地随机读取。"""
        return True

    def tell(self):
        """Return the local cursor. / 返回本地游标。"""
        return self.position

    def seek(self, offset, whence=0):
        """Move a cursor without fetching anything. / 移动游标，不获取任何数据。"""
        self._checkClosed()
        if whence not in (0, 1, 2):
            raise ValueError("invalid raster seek mode")
        position = offset + (0 if whence == 0 else self.position if whence == 1 else self.source.size)
        if position < 0:
            raise ValueError("negative raster offset")
        self.position = position
        return position

    def read(self, size=-1):
        """Assemble a bounded read from explicit local files. / 从明确的本地文件组装有界读取。"""
        self._checkClosed()
        try:
            return self._read(size)
        except (OSError, ValueError):
            self.source.failed_reads = True
            if self.gdal_callback:
                # C callbacks must return short reads; the caller rejects the failure flag. / C 回调返回短读，由调用者拒绝失败标记。
                return b""
            raise

    def _read(self, size):
        """Read strict local ranges without filling gaps. / 严格读取本地范围，不填补空缺。"""
        size = self.source.size - self.position if size is None or size < 0 else size
        size = max(0, min(size, self.source.size - self.position))
        if size > MAX_READ_BYTES:
            raise OSError("raster read exceeds bounded decoder allowance")
        chunks, remaining = [], size
        while remaining:
            start = self.position // self.source.block_bytes * self.source.block_bytes
            if start not in self.source.rows:
                self.source.missing.add(start)
                raise OSError(f"required local TIFF block is missing: {start}")
            row = self.source.rows[start]
            path = (self.source.root / row["file"]).resolve()
            if not path.is_relative_to(self.source.root) or ".." in Path(row["file"]).parts:
                raise OSError("TIFF block is outside data root")
            cache_key = (str(path), row["sha256"])
            block = self.source.blocks.pop(cache_key, None)
            if block is None:
                block = path.read_bytes()
                if len(block) != row["range"][1] - start + 1 or hashlib.sha256(block).hexdigest() != row["sha256"]:
                    raise OSError("TIFF block changed after provenance verification")
            if len(block) <= MAX_BLOCK_CACHE_BYTES:
                self.source.blocks[cache_key] = block
                while sum(map(len, self.source.blocks.values())) > MAX_BLOCK_CACHE_BYTES:
                    self.source.blocks.popitem(last=False)
            offset = self.position - start
            count = min(remaining, len(block) - offset)
            if count <= 0:
                raise OSError("incomplete local TIFF block")
            chunks.append(block[offset:offset + count])
            self.position += count
            remaining -= count
        return b"".join(chunks)

    def readinto(self, buffer):
        """Fill a caller buffer from local bytes. / 用本地字节填充调用者缓冲。"""
        value = self.read(len(buffer))
        buffer[:len(value)] = value
        return len(value)


class LocalRaster:
    """Offer exactly one TIFF name to GDAL with no sidecar or URL fallback.

只向 GDAL 提供一个 TIFF 名称，不回退到附属文件或 URL。
"""

    def __init__(self, manifest, root, cache):
        self.size = manifest["source"]["imagery_published_bytes"]
        self.block_bytes = manifest["block_bytes"]
        self.rows = {row["range"][0]: row for row in manifest["ranges"]}
        self.root = root.resolve()
        self.blocks = cache.setdefault("p6_local_tiff_blocks", OrderedDict())
        self.missing = set()
        self.failed_reads = False
        self.name = "p6-local-source.tif"

    def __call__(self, path, mode="rb"):
        """Refuse everything except this read-only virtual TIFF. / 拒绝只读虚拟 TIFF 以外的一切请求。"""
        if str(path) != self.name or mode not in {"r", "rb"}:
            raise FileNotFoundError("only the bound local TIFF is available")
        return RangeFile(self, gdal_callback=True)


def verify_window(receipt: dict, manifest: dict, data_root: Path, cache: dict, manifest_sha256: str) -> None:
    """Decode real source bytes and recompute native RGB, JPEG and alpha digests.

解码实际源字节并重算原生 RGB、JPEG 与 alpha 摘要。
"""
    x, y, width, height = receipt["native_window_xywh"]
    if (any(type(v) is not int for v in (x, y, width, height)) or min(x, y) < 0 or min(width, height) <= 0
            or width * height > MAX_WINDOW_PIXELS):
        raise ValueError("native window exceeds the 60 MP decoder bound")
    key = ("p6_decoded_source_window", manifest_sha256, x, y, width, height)
    if key not in cache:
        try:
            import numpy as np
            import rasterio
            from PIL import Image
            from rasterio.enums import ColorInterp
            from rasterio.windows import Window
        except ImportError as exc:
            raise ValueError("the pinned local raster decoder is unavailable") from exc
        source = LocalRaster(manifest, data_root, cache)
        # A custom opener prevents eager full-file reads and all network access. / 自定义 opener 避免全文件预读及任何联网。
        try:
            with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_PAM_ENABLED="NO"):
                with rasterio.open(source.name, "r", driver="GTiff", opener=source, sharing=False) as dataset:
                    if (dataset.driver != "GTiff" or dataset.count != 4 or dataset.dtypes != ("uint8",) * 4
                            or dataset.colorinterp != (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
                            or x + width > dataset.width or y + height > dataset.height):
                        raise ValueError("actual TIFF format, channels or window bounds disagree")
                    window = Window(x, y, width, height)
                    pixels = dataset.read([1, 2, 3, 4], window=window)
                    if source.failed_reads or pixels.shape != (4, height, width):
                        raise ValueError("not all required local TIFF bytes were available")
                    rgb = np.ascontiguousarray(pixels[:3].transpose(1, 2, 0))
                    alpha = pixels[3]
                    jpeg, mask = io.BytesIO(), io.BytesIO()
                    Image.fromarray(rgb).save(jpeg, format="JPEG", quality=98, subsampling=0)
                    Image.fromarray(alpha).save(mask, format="PNG")
                    actual = {"width": dataset.width, "height": dataset.height, "crs": str(dataset.crs),
                              "transform": list(dataset.transform), "window_transform": list(dataset.window_transform(window)),
                              "native_rgb_sha256": hashlib.sha256(rgb.tobytes()).hexdigest(),
                              "image_sha256": hashlib.sha256(jpeg.getvalue()).hexdigest(),
                              "mask_sha256": hashlib.sha256(mask.getvalue()).hexdigest(),
                              "valid_pixel_fraction": float(np.count_nonzero(alpha) / alpha.size)}
        except (OSError, rasterio.errors.RasterioError) as exc:
            raise ValueError("local TIFF bytes could not reproduce the declared window") from exc
        cache[key] = actual
    actual = cache[key]
    raster = receipt["raster"]
    if (any(actual[k] != receipt[k] for k in ("native_rgb_sha256", "image_sha256", "mask_sha256"))
            or any(actual[k] != raster[k] for k in ("width", "height", "crs"))
            or len(raster["transform"]) != 9 or len(receipt["window_transform"]) != 9
            or any(not math.isclose(a, b, rel_tol=0, abs_tol=1e-8) for a, b in zip(actual["transform"], raster["transform"], strict=True))
            or any(not math.isclose(a, b, rel_tol=0, abs_tol=1e-8) for a, b in zip(actual["window_transform"], receipt["window_transform"], strict=True))
            or not math.isclose(actual["valid_pixel_fraction"], receipt["valid_pixel_fraction"], abs_tol=1e-9)):
        raise ValueError("actual local TIFF pixels or georeferencing do not match the extraction")
