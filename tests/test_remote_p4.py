"""The cloud install of the frozen S2 dataset (D065): only the manifest's exact files, never links or traversal.

冻结 S2 数据集的云端安装（D065）：只接受清单中的确切文件，从不接受链接或目录穿越。
"""

from __future__ import annotations

import hashlib
import io
import json
import runpy
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def runner():
    return runpy.run_path(str(ROOT / "scripts/remote_p4.py"))


def workspace(tmp_path: Path, files: dict[str, bytes], *, link: bool = False) -> tuple[Path, Path, str]:
    deployment = tmp_path / "releases" / "d1"
    manifest = deployment / "source/eval/s2/visa_pcb_v1/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"samples": [
        {"sample_id": name, "file": name, "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in {"original/a.jpg": b"a", "derived/b.jpg": b"b"}.items()]}))
    upload = tmp_path / "incoming" / "20260927T000000Z-00000000"
    upload.mkdir(parents=True)
    archive = upload / "visa_pcb_v1.tar"
    with tarfile.open(archive, "w") as stream:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            stream.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo("original/link.jpg")
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            stream.addfile(info)
    return deployment, archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def test_the_dataset_installs_read_only_after_a_file_by_file_check(tmp_path):
    deployment, _, digest = workspace(tmp_path, {"original/a.jpg": b"a", "derived/b.jpg": b"b"})
    result = runner()["install_data"](tmp_path, deployment, {"run_id": "20260927T000000Z-00000000",
                                                             "sha256": digest})
    assert result["status"] == "installed" and result["verification"]["status"] == "passed"
    installed = tmp_path / "data/visa_pcb_v1/original/a.jpg"
    assert installed.read_bytes() == b"a" and not installed.stat().st_mode & 0o222
    again = runner()["install_data"](tmp_path, deployment, {"run_id": "20260927T000000Z-00000000",
                                                            "sha256": digest})
    assert again["already_installed"] is True and again["status"] == "installed"


@pytest.mark.parametrize("files,link,match", [({"original/a.jpg": b"a", "derived/b.jpg": b"x"}, False,
                                               "verification failed"),
                                              ({"original/a.jpg": b"a"}, True, "unsafe"),
                                              ({"../escape.jpg": b"a"}, False, "unsafe")])
def test_a_changed_file_a_link_or_a_traversal_is_refused(tmp_path, files, link, match):
    deployment, _, digest = workspace(tmp_path, files, link=link)
    with pytest.raises((ValueError, RuntimeError), match=match):
        runner()["install_data"](tmp_path, deployment, {"run_id": "20260927T000000Z-00000000", "sha256": digest})
    assert not (tmp_path / "data/visa_pcb_v1").exists()


def test_the_archive_digest_must_match_the_upload(tmp_path):
    deployment, _, _ = workspace(tmp_path, {"original/a.jpg": b"a", "derived/b.jpg": b"b"})
    with pytest.raises(ValueError, match="digest"):
        runner()["install_data"](tmp_path, deployment, {"run_id": "20260927T000000Z-00000000", "sha256": "0" * 64})
