"""Record and check the immutable v1 field numbers, enums and RPC signatures.

记录并检查 v1 不可变的字段号、枚举与 RPC 签名。
"""

import argparse
import json
import runpy
import tempfile
from pathlib import Path

from google.protobuf import descriptor_pb2

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "proto/v1-wire-lock.json"


def describe(descriptor_path: Path):
    descriptor = descriptor_pb2.FileDescriptorSet.FromString(descriptor_path.read_bytes())
    result = {"format": "drone.wire-lock/v1", "messages": {}, "enums": {}, "services": {}}
    for file in descriptor.file:
        if not file.package.startswith("drone."):
            continue
        for message in file.message_type:
            result["messages"][file.package + "." + message.name] = {
                field.name: {
                    "number": field.number,
                    "type": field.type,
                    "type_name": field.type_name,
                    "label": field.label,
                    "optional": field.proto3_optional,
                }
                for field in message.field
            }
        for enum in file.enum_type:
            result["enums"][file.package + "." + enum.name] = {value.name: value.number for value in enum.value}
        for service in file.service:
            result["services"][file.package + "." + service.name] = {
                method.name: {
                    "input": method.input_type,
                    "output": method.output_type,
                    "client_streaming": method.client_streaming,
                    "server_streaming": method.server_streaming,
                }
                for method in service.method
            }
    return result


def check(frozen, current):
    for section in ("messages", "enums", "services"):
        for name, members in frozen[section].items():
            for member, signature in members.items():
                if current[section].get(name, {}).get(member) != signature:
                    raise ValueError(f"breaking frozen v1 {section}: {name}.{member}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        path = runpy.run_path(str(ROOT / "scripts/generate_proto.py"))["generate"](Path(directory))
        current = describe(path)
    if args.check:
        check(json.loads(LOCK.read_text(encoding="utf-8")), current)
    elif LOCK.exists():
        raise SystemExit("v1 is already frozen; a new wire major requires an explicit migration")
    else:
        LOCK.write_text(json.dumps(current, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print("v1 wire compatibility verified")


if __name__ == "__main__":
    main()
