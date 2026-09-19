"""Durable append-only authority log; uncertain writes never become receipts.

持久化的只追加权威日志；不确定写入永远不变成确定回执。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from drone_agent.contracts import utcnow


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def durable_artifact(path: Path, data: bytes):
    with path.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name == "posix":
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows, previous = [], "0" * 64
    for index, line in enumerate(path.read_bytes().splitlines(keepends=True)):
        if not line.endswith(b"\n"):
            raise ValueError("torn authority log; operator reconciliation required")
        row = json.loads(line)
        claimed = row.pop("sha256")
        if row["seq"] != index or row["previous"] != previous or content_hash(row) != claimed:
            raise ValueError("authority log integrity failure")
        row["sha256"] = previous = claimed
        rows.append(row)
    return rows


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.stream = path.open("a+b")
        self.lock = path.with_name(path.name + ".lock").open("a+b")
        # Lock one writer across processes; readers consume complete records. / 跨进程锁定单写者；读取者只消费完整记录。
        if os.name == "posix":
            import fcntl

            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt

            self.lock.seek(0)
            msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
        self.rows = read_log(path)

    def append(self, kind: str, data: dict) -> dict:
        row = {
            "seq": len(self.rows),
            "previous": self.rows[-1]["sha256"] if self.rows else "0" * 64,
            "timestamp": utcnow().isoformat(),
            "monotonic_ns": time.monotonic_ns(),
            "kind": kind,
            "data": data,
        }
        row["sha256"] = content_hash(row)
        self.stream.seek(0, os.SEEK_END)
        self.stream.write(canonical(row) + b"\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.rows.append(row)
        return row

    def close(self):
        self.stream.close()
        self.lock.close()


class CommandLedger:
    def __init__(self, journal: Journal):
        self.journal = journal
        self.highest_epoch = -1
        self.last_seq = -1
        self.commands: dict[str, dict] = {}
        self.lease: dict | None = None
        for row in journal.rows:
            self._apply(row["kind"], row["data"])
        # No lease survives process restart; history and receipts do. / 租约不跨重启存活；代次历史与回执保留。
        self.lease = None

    def _apply(self, kind, data):
        if kind == "lease":
            epoch = data["lease_epoch"]
            if epoch > self.highest_epoch:
                self.last_seq = -1
            self.highest_epoch = max(self.highest_epoch, epoch)
            self.lease = data
        elif kind == "revoked":
            self.lease = None
        elif kind == "intent":
            self.commands[data["key"]] = {"receipt": "unknown", **data}
            self.last_seq = max(self.last_seq, data["envelope"]["command_seq"])
        elif kind == "receipt":
            self.commands[data["key"]].update(data)

    def record(self, kind, data):
        self.journal.append(kind, data)
        self._apply(kind, data)

    def reconcile(self, key: str) -> dict:
        return self.commands.get(key, {"receipt": "not_received"}).copy()
