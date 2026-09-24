"""Compile the protos once per session for the autonomy protocol tests.

为自主层协议测试在每个会话编译一次 proto。
"""

import runpy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session", autouse=True)
def autonomy_stubs(tmp_path_factory):
    target = tmp_path_factory.mktemp("autonomy-stubs")
    runpy.run_path(str(ROOT / "scripts/generate_proto.py"))["generate"](target)
    sys.path.insert(0, str(target))
    yield target
    sys.path.remove(str(target))
