"""Compile all repository proto files into ignored, local artifacts.

将仓库内所有 proto 编译为被忽略的本地产物。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import grpc_tools
from grpc_tools import protoc

ROOT = Path(__file__).resolve().parents[1]


def generate(output: Path) -> Path:
    """Build Python stubs and a descriptor set without starting any service.

    生成 Python stub 与描述符集合，不启动任何服务。
    """
    output.mkdir(parents=True, exist_ok=True)
    descriptor = output / "contracts.pb"
    proto_root = ROOT / "proto"
    sources = sorted(proto_root.rglob("*.proto"))
    result = protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{proto_root}",
            f"-I{Path(grpc_tools.__file__).parent / '_proto'}",
            f"--python_out={output}",
            f"--grpc_python_out={output}",
            f"--descriptor_set_out={descriptor}",
            "--include_imports",
            *[str(path) for path in sources],
        ]
    )
    if result:
        raise RuntimeError(f"protoc failed with exit code {result}")
    return descriptor


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "gen")
    args = parser.parse_args()
    print(generate(args.output.resolve()))
