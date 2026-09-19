"""Copy only simulation inputs to an owned ASCII workspace; never copy secrets.

仅将仿真输入复制到本任务的 ASCII 工作区；不复制密钥文件。
"""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ("Dockerfile", "compose.yaml", "entrypoint.sh", "smoke.py")


def stage(destination: Path) -> None:
    destination = destination.resolve()
    if not str(destination).isascii():
        raise ValueError("simulation workspace must have an ASCII path")
    marker = destination / "drone-agent-m0-stage.json"
    if destination.exists() and any(destination.iterdir()):
        if not marker.exists() or json.loads(marker.read_text(encoding="utf-8")).get("source") != str(ROOT):
            raise ValueError("refusing to overwrite a directory not owned by this staging task")
    destination.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name in FILES:
        source = ROOT / "sim" / name
        shutil.copyfile(source, destination / name)
        hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    (destination / "artifacts").mkdir(exist_ok=True)
    marker.write_text(json.dumps({"source": str(ROOT), "sha256": hashes}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    stage(parser.parse_args().destination)
