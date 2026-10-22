"""Exercise bounded complete-file replay with synthetic bytes and native pixels only.

仅用合成字节与原生像素验证有界完整文件回放。
"""
from __future__ import annotations

import hashlib
import io
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

R = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/p6_raster_replay.py"))
G = R["verify_full_window"].__globals__


@pytest.fixture
def small_limits(monkeypatch):
    monkeypatch.setitem(G, "_FULL_RASTER_CHUNK_BYTES", 16)
    monkeypatch.setitem(G, "MAX_BLOCK_CACHE_BYTES", 32)


def full_source(root, payload, cache=None):
    parent = root / "parent.tif"
    parent.write_bytes(payload)
    return R["_FullRaster"](parent, hashlib.sha256(payload).hexdigest(), root, {} if cache is None else cache)


def test_production_decoder_limits_remain_bounded():
    assert G["MAX_WINDOW_PIXELS"] == 60_000_000
    assert G["MAX_READ_BYTES"] == 256 * 1024 * 1024
    assert G["MAX_BLOCK_CACHE_BYTES"] == 64 * 1024 * 1024
    assert G["_FULL_RASTER_CHUNK_BYTES"] == 4 * 1024 * 1024


def test_full_file_reads_cross_chunks_and_short_final_chunk_exactly(tmp_path, small_limits):
    payload = bytes(range(79))
    source = full_source(tmp_path, payload)
    reader = source._reader(source)
    reader.seek(14)
    assert reader.read(39) == payload[14:53]
    assert sum(map(len, source.blocks.values())) <= 32
    reader.seek(29)
    buffer = bytearray(27)
    assert reader.readinto(buffer) == 27
    assert buffer == payload[29:56]
    reader.seek(73)
    assert reader.read(20) == payload[73:]
    assert reader.tell() == len(payload)
    assert reader.read() == b""
    assert source.missing == set() and source.failed_reads is False


def test_small_reads_reuse_chunks_and_eviction_rereads_only_one_chunk(tmp_path, small_limits, monkeypatch):
    payload = bytes(range(79))
    parent = tmp_path / "parent.tif"
    parent.write_bytes(payload)
    parent_sha256 = hashlib.sha256(payload).hexdigest()
    stats = {"read_bytes": 0, "hash_bytes": 0, "requests": []}
    real_open, real_sha256 = Path.open, hashlib.sha256

    class CountedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def seek(self, *args):
            return self.stream.seek(*args)

        def read(self, size=-1):
            value = self.stream.read(size)
            stats["requests"].append(size)
            stats["read_bytes"] += len(value)
            return value

    class CountedDigest:
        def __init__(self, value=b""):
            self.digest = real_sha256(value)
            stats["hash_bytes"] += len(value)

        def update(self, value):
            self.digest.update(value)
            stats["hash_bytes"] += len(value)

        def hexdigest(self):
            return self.digest.hexdigest()

    def counted_open(path, *args, **kwargs):
        stream = real_open(path, *args, **kwargs)
        return CountedReader(stream) if path == parent else stream

    monkeypatch.setattr(Path, "open", counted_open)
    monkeypatch.setitem(G, "hashlib", SimpleNamespace(sha256=CountedDigest))
    source = R["_FullRaster"](parent, parent_sha256, tmp_path, {})
    reader = source._reader(source)
    assert stats["read_bytes"] == len(payload)
    assert stats["hash_bytes"] == 2 * len(payload)
    reader.seek(2)
    assert reader.read(2) == payload[2:4]
    first = stats.copy()
    for offset in (2, 4, 6, 8) * 5:
        reader.seek(offset)
        assert reader.read(2) == payload[offset:offset + 2]
    assert stats["read_bytes"] == first["read_bytes"] == len(payload) + 16
    assert stats["hash_bytes"] == first["hash_bytes"] == 2 * len(payload) + 16
    reader.seek(14)
    assert reader.read(4) == payload[14:18]
    reader.seek(34)
    assert reader.read(1) == payload[34:35]
    reader.seek(2)
    assert reader.read(1) == payload[2:3]
    assert stats["read_bytes"] == len(payload) + 4 * 16
    assert stats["hash_bytes"] == 2 * len(payload) + 4 * 16
    assert all(0 < size <= 16 for size in stats["requests"])
    assert sum(map(len, source.blocks.values())) == 32


def test_chunk_lru_shares_the_existing_range_byte_budget(tmp_path, small_limits):
    cache = {"p6_local_tiff_blocks": R["OrderedDict"]([(("range.bin", "digest"), b"x" * 32)])}
    source = full_source(tmp_path, bytes(range(79)), cache)
    reader = source._reader(source)
    assert reader.read(1) == b"\x00"
    assert ("range.bin", "digest") not in source.blocks
    assert source.blocks is cache["p6_local_tiff_blocks"]
    reader.seek(16)
    assert reader.read(1) == b"\x10"
    reader.seek(32)
    assert reader.read(1) == b"\x20"
    assert sum(map(len, source.blocks.values())) == 32


def test_equal_chunk_digests_do_not_alias_different_file_offsets(tmp_path, small_limits):
    payload = b"a" * 32 + b"b" * 16
    source = full_source(tmp_path, payload)
    reader = source._reader(source)
    assert reader.read(1) == b"a"
    source.parent.write_bytes(payload[:16] + b"z" * 16 + payload[32:])
    reader.seek(16)
    with pytest.raises(OSError, match="changed after provenance"):
        reader.read(1)
    assert source.failed_reads is True


@pytest.mark.parametrize("change", ["modify", "truncate"])
@pytest.mark.parametrize("callback", [False, True])
def test_uncached_chunk_changes_are_rejected_after_a_successful_read(tmp_path, small_limits, change, callback):
    payload = bytes(range(79))
    source = full_source(tmp_path, payload)
    reader = source._reader(source, gdal_callback=callback)
    assert reader.read(1) == payload[:1]
    changed = payload[:16] + b"z" * 16 + payload[32:] if change == "modify" else payload[:18]
    source.parent.write_bytes(changed)
    reader.seek(17)
    if callback:
        assert reader.read(2) == b""
    else:
        with pytest.raises(OSError, match="changed after provenance"):
            reader.read(2)
    assert source.failed_reads is True and source.missing == set()


def test_evicted_chunk_is_digest_checked_again(tmp_path, small_limits):
    payload = bytes(range(79))
    source = full_source(tmp_path, payload)
    reader = source._reader(source)
    for offset in (0, 16, 32):
        reader.seek(offset)
        assert reader.read(1) == payload[offset:offset + 1]
    source.parent.write_bytes(b"z" + payload[1:])
    reader.seek(0)
    with pytest.raises(OSError, match="changed after provenance"):
        reader.read(1)


def test_missing_private_chunk_sets_missing_and_failed_read_flags(tmp_path, small_limits):
    source = full_source(tmp_path, bytes(range(79)))
    source._chunk_sha256.pop(16)
    reader = source._reader(source)
    reader.seek(16)
    with pytest.raises(OSError, match="block is missing"):
        reader.read(1)
    assert source.failed_reads is True and source.missing == {16}


def test_wrong_complete_parent_digest_is_rejected_before_decoding(tmp_path, small_limits):
    parent = tmp_path / "parent.tif"
    parent.write_bytes(bytes(range(79)))
    with pytest.raises(ValueError, match="changed after provenance"):
        R["verify_full_window"]({}, parent, "0" * 64, tmp_path, {})


@pytest.mark.parametrize("size", [0, 33])
def test_complete_parent_size_guard_precedes_file_reads(tmp_path, monkeypatch, size):
    parent = tmp_path / "parent.tif"
    parent.write_bytes(b"x" * size)
    monkeypatch.setitem(G, "MAX_READ_BYTES", 32)

    def refused_open(*args, **kwargs):
        raise AssertionError("the size guard must reject before reading")

    monkeypatch.setattr(Path, "open", refused_open)
    with pytest.raises(ValueError, match="bounded source allowance"):
        R["verify_full_window"]({}, parent, "0" * 64, tmp_path, {})


def test_complete_parent_outside_root_is_rejected_before_reading(tmp_path, monkeypatch):
    parent, root = tmp_path / "outside.tif", tmp_path / "data"
    root.mkdir()
    parent.write_bytes(b"outside")

    def refused_open(*args, **kwargs):
        raise AssertionError("outside data must never be opened")

    monkeypatch.setattr(Path, "open", refused_open)
    with pytest.raises(ValueError, match="outside the data root"):
        R["verify_full_window"]({}, parent, "0" * 64, root, {})


@pytest.mark.parametrize("path,mode", [("p6-local-source.tif.msk", "rb"), ("p6-local-source.tif.aux.xml", "rb"),
    ("https://example.test/parent.tif", "rb"), ("other.tif", "rb"), ("p6-local-source.tif", "wb")])
def test_complete_view_refuses_sidecars_urls_and_writes(tmp_path, small_limits, path, mode):
    source = full_source(tmp_path, bytes(range(79)))
    with pytest.raises(FileNotFoundError, match="only the bound local TIFF"):
        source(path, mode)


def test_original_range_files_still_replay_cross_file_reads(tmp_path, small_limits):
    payload = bytes(range(27))
    rows = []
    for start in range(0, len(payload), 8):
        block = payload[start:start + 8]
        path = tmp_path / f"range-{start}.bin"
        path.write_bytes(block)
        rows.append({"range": [start, start + len(block) - 1], "file": path.name, "sha256": hashlib.sha256(block).hexdigest()})
    source = R["LocalRaster"]({"source": {"imagery_published_bytes": len(payload)}, "block_bytes": 8, "ranges": rows}, tmp_path, {})
    reader = R["RangeFile"](source)
    reader.seek(6)
    assert reader.read(20) == payload[6:26]
    assert sum(map(len, source.blocks.values())) <= 32
    source.rows.pop(24)
    reader.seek(24)
    with pytest.raises(OSError, match="block is missing"):
        reader.read(1)
    assert source.missing == {24} and source.failed_reads is True


def test_range_json_cannot_enable_private_complete_file_offsets(tmp_path, small_limits):
    payload = bytes(range(79))
    parent = tmp_path / "parent.tif"
    parent.write_bytes(payload)
    manifest = {"source": {"imagery_published_bytes": len(payload)}, "block_bytes": 16,
        "parent_sha256": hashlib.sha256(payload).hexdigest(), "full_object": True,
        "_chunk_sha256": {0: hashlib.sha256(payload[16:32]).hexdigest()},
        "ranges": [{"range": [0, 15], "file": parent.name, "file_offset": 16,
                    "sha256": hashlib.sha256(payload[16:32]).hexdigest()}]}
    source = R["LocalRaster"](manifest, tmp_path, {})
    with pytest.raises(OSError, match="changed after provenance"):
        R["RangeFile"](source).read(1)
    assert source.failed_reads is True


def test_original_range_path_traversal_is_still_rejected(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (tmp_path / "outside.bin").write_bytes(b"outside")
    manifest = {"source": {"imagery_published_bytes": 7}, "block_bytes": 7,
        "ranges": [{"range": [0, 6], "file": "../outside.bin", "sha256": hashlib.sha256(b"outside").hexdigest()}]}
    source = R["LocalRaster"](manifest, root, {})
    with pytest.raises(OSError, match="outside data root"):
        R["RangeFile"](source).read(1)
    assert source.failed_reads is True


def test_native_rgb_internal_mask_and_geometry_reproduce_across_private_chunks(tmp_path, monkeypatch):
    import numpy as np
    import rasterio
    from affine import Affine
    from PIL import Image
    from rasterio.enums import ColorInterp
    from rasterio.io import MemoryFile

    yy, xx = np.indices((32, 40))
    pixels = np.stack((xx * 5, yy * 7, (xx + yy) * 3)).astype("uint8")
    mask = np.full((32, 40), 255, dtype="uint8")
    mask[:7, :9] = 0
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), MemoryFile() as memory:
        with memory.open(driver="GTiff", width=40, height=32, count=3, dtype="uint8", crs="EPSG:32630",
                         transform=Affine(1, 0, 100, 0, -1, 200), photometric="RGB", tiled=True, blockxsize=16, blockysize=16) as dataset:
            dataset.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue)
            dataset.write(pixels)
            dataset.write_mask(mask)
        payload = memory.read()
    parent = tmp_path / "parent.tif"
    parent.write_bytes(payload)
    native = np.ascontiguousarray(pixels[:, 3:23, 2:26].transpose(1, 2, 0))
    alpha = mask[3:23, 2:26]
    jpeg, png = io.BytesIO(), io.BytesIO()
    Image.fromarray(native).save(jpeg, format="JPEG", quality=98, subsampling=0)
    Image.fromarray(alpha).save(png, format="PNG")
    receipt = {"native_window_xywh": [2, 3, 24, 20], "native_rgb_sha256": hashlib.sha256(native.tobytes()).hexdigest(),
        "image_sha256": hashlib.sha256(jpeg.getvalue()).hexdigest(), "mask_sha256": hashlib.sha256(png.getvalue()).hexdigest(),
        "raster": {"width": 40, "height": 32, "crs": "EPSG:32630", "transform": [1, 0, 100, 0, -1, 200, 0, 0, 1]},
        "window_transform": [1, 0, 102, 0, -1, 197, 0, 0, 1], "valid_pixel_fraction": float(np.count_nonzero(alpha) / alpha.size)}
    monkeypatch.setitem(G, "_FULL_RASTER_CHUNK_BYTES", 256)
    monkeypatch.setitem(G, "MAX_BLOCK_CACHE_BYTES", 1024)
    cache = {}
    R["verify_full_window"](receipt, parent, hashlib.sha256(payload).hexdigest(), tmp_path, cache)
    assert 0 < sum(map(len, cache["p6_local_tiff_blocks"].values())) <= 1024
