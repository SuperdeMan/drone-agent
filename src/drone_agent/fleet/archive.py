"""Archive of old `status.accepted` rows: verifiable gzip members out of the ledger, chunk by chunk (D085).

A dock report the service accepts writes one `status.accepted` audit row, about one per dock per second; nothing
else in `op_events` comes close. Rows older than the retention move, at most one chunk per call, into a per-day file
`status-accepted-YYYYMMDD.jsonl.gz`: the chunk is appended as one gzip member and synced, then one transaction records
its file, byte range, id range, row count and SHA-256 in `cap_event_archives` and deletes exactly those rows. The
ledger rows and the archived chunks are together the complete record at every moment. A member written but never
recorded (a crash between the two) is cut off before the next chunk is appended. No other event kind is touched.

Every call does bounded work however long the history is. `created_at` has no index, so the rows due are found by a
binary search over ids for the last row before the cutoff (the service writes `op_events` in time order; a row whose
clock stepped back behind its id is skipped, never lost), and only that id range is read. A chunk is archived only when
it is complete: full, or its day is over. After a call that finds nothing complete the archive rests a while.

旧 `status.accepted` 行的归档：可核验的 gzip 成员，逐块移出账本（D085）。服务每接受一条机场报告写一行 `status.accepted`
审计行，约每个机场每秒一行，`op_events` 中其他事件都远不及此。超过保留期的行每次调用至多移出一块，进入按日的文件
`status-accepted-YYYYMMDD.jsonl.gz`：该块作为一个 gzip 成员追加并同步落盘，随后一个事务在 `cap_event_archives` 中登记其
文件、字节范围、id 范围、行数与 SHA-256，并恰好删除这些行。任何时刻账本行与已归档块合起来都是完整记录。写入但未登记的
成员（两步之间崩溃）在追加下一块之前被截掉。不触碰任何其他事件类型。

无论历史多长，每次调用的工作量都有上限。`created_at` 没有索引，因此按 id 二分查找截止时刻之前的最后一行（服务按时间顺序写入
`op_events`；时钟回拨而落在 id 之后的行会被跳过，不会丢失），只读取这段 id 范围。只有完整的块才归档：满块，或该日已结束。
一次调用没有找到完整的块后，归档会休息一段时间。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from datetime import timedelta
from pathlib import Path

from drone_agent.contracts import utcnow

KIND = "status.accepted"
IDLE_S = 30.0


def _line(row: dict) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


class StatusArchive:
    """Moves `status.accepted` rows older than `retention_days` out of the ledger (D085).

    把早于 `retention_days` 的 `status.accepted` 行移出账本（D085）。
    """

    def __init__(self, ledger, directory: Path, *, retention_days: float, chunk_rows: int = 2000, clock=utcnow,
                 monotonic=time.monotonic):
        if retention_days <= 0:
            raise ValueError("the retention must be positive")
        self.ledger, self.directory, self.clock, self.monotonic = ledger, Path(directory), clock, monotonic
        self.retention = timedelta(days=retention_days)
        self.chunk_rows = chunk_rows
        self._last_id: int | None = None
        self._idle_until = 0.0

    def _cursor(self) -> int:
        """The last archived id; read once, then kept (this archive is the only writer). / 最后已归档的 id；读一次后保留。"""
        if self._last_id is None:
            row = self.ledger._one("SELECT MAX(last_id) AS top FROM cap_event_archives WHERE kind=?", (KIND,))
            self._last_id = int(row["top"] or 0) if row else 0
        return self._last_id

    def _end(self, name: str) -> int:
        row = self.ledger._one("SELECT MAX(offset + length) AS end FROM cap_event_archives WHERE file=?", (name,))
        return int(row["end"] or 0) if row else 0

    def _first(self, at_least: int) -> dict | None:
        return self.ledger._one("SELECT id, created_at FROM op_events WHERE id >= ? ORDER BY id LIMIT 1", (at_least,))

    def _boundary(self, cursor: int, cutoff: str) -> int | None:
        """The last id before the cutoff, by a binary search over ids (rowid lookups only); None when none is due.

        截止时刻之前的最后一个 id，按 id 二分查找（只用 rowid 查找）；没有到期行时为 None。
        """
        first = self._first(cursor + 1)
        if first is None or first["created_at"] >= cutoff:
            return None
        top = self.ledger._one("SELECT id, created_at FROM op_events ORDER BY id DESC LIMIT 1")
        if top["created_at"] < cutoff:
            return top["id"]
        low, high = first["id"] + 1, top["id"]  # the first row at or after `high` is not due / `high` 起的首行未到期
        while low < high:
            middle = (low + high) // 2
            if self._first(middle)["created_at"] >= cutoff:
                high = middle
            else:
                low = middle + 1
        stop = self._first(low)["id"]
        return self.ledger._one("SELECT MAX(id) AS id FROM op_events WHERE id < ?", (stop,))["id"]

    def due(self) -> int:
        """Rows old enough to archive, counted within the bounded id range. / 在有界 id 范围内统计可归档行数。"""
        cutoff = (self.clock() - self.retention).isoformat()
        cursor = self._cursor()
        boundary = self._boundary(cursor, cutoff)
        if boundary is None:
            return 0
        row = self.ledger._one("SELECT COUNT(*) AS n FROM op_events WHERE id > ? AND id <= ? AND kind=? "
                               "AND created_at < ?", (cursor, boundary, KIND, cutoff))
        return int(row["n"]) if row else 0

    def step(self) -> dict | None:
        """Archive at most one complete chunk; the chunk's record, or None when none is complete.

        至多归档一个完整的块；返回该块的记录，没有完整的块时返回 None。
        """
        if self.monotonic() < self._idle_until:
            return None
        cutoff = (self.clock() - self.retention).isoformat()
        cursor = self._cursor()
        boundary = self._boundary(cursor, cutoff)
        rows = [] if boundary is None else self.ledger._rows(
            "SELECT id, subject, kind, actor, body, created_at FROM op_events WHERE id > ? AND id <= ? AND kind=? "
            "AND created_at < ? ORDER BY id LIMIT ?", (cursor, boundary, KIND, cutoff, self.chunk_rows))
        if not rows:
            self._idle_until = self.monotonic() + IDLE_S
            return None
        day = rows[0]["created_at"][:10]
        # One chunk never crosses a day, so one member lives in exactly one file; it stays a contiguous id range.
        # 一块不跨日，一个成员只在一个文件中；块保持连续的 id 范围。
        cut = next((i for i, row in enumerate(rows) if row["created_at"][:10] != day), len(rows))
        if cut == len(rows) < self.chunk_rows:
            # Short of a full chunk: archive it only once a later status row shows its day is over.
            # 不足一整块：只有更晚的状态行表明该日已结束时才归档。
            later = self.ledger._one("SELECT created_at FROM op_events WHERE id > ? AND kind=? ORDER BY id LIMIT 1",
                                     (rows[-1]["id"], KIND))
            if later is None or later["created_at"][:10] == day:
                self._idle_until = self.monotonic() + IDLE_S
                return None
        rows = rows[:cut]
        name = f"status-accepted-{day.replace('-', '')}.jsonl.gz"
        path = self.directory / name
        self.directory.mkdir(parents=True, exist_ok=True)
        member = gzip.compress("".join(_line(row) for row in rows).encode("utf-8"), compresslevel=6, mtime=0)
        offset = self._end(name)
        with open(path, "ab+") as stream:
            # Cut off a member that was written but never recorded. / 截掉写入但未登记的成员。
            stream.truncate(offset)
            stream.seek(offset)
            stream.write(member)
            stream.flush()
            os.fsync(stream.fileno())
        record = {"kind": KIND, "file": name, "offset": offset, "length": len(member), "first_id": rows[0]["id"],
                  "last_id": rows[-1]["id"], "rows": len(rows), "first_at": rows[0]["created_at"],
                  "last_at": rows[-1]["created_at"], "sha256": hashlib.sha256(member).hexdigest()}
        ids = [row["id"] for row in rows]
        with self.ledger.transaction() as db:
            db.execute("INSERT INTO cap_event_archives (kind, file, offset, length, first_id, last_id, rows, first_at, "
                       "last_at, sha256, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (*record.values(), self.clock().isoformat()))
            marks = ",".join("?" * len(ids))
            deleted = db.execute(f"DELETE FROM op_events WHERE kind=? AND id IN ({marks})", (KIND, *ids)).rowcount
            if deleted != len(ids):
                raise RuntimeError(f"archived {len(ids)} rows but deleted {deleted}; rolled back")
        self._last_id = record["last_id"]
        return record

    def run(self, max_chunks: int) -> list[dict]:
        """Up to `max_chunks` chunks, oldest first. / 至多 `max_chunks` 块，最旧的先。"""
        done = []
        for _ in range(max_chunks):
            record = self.step()
            if record is None:
                break
            done.append(record)
        return done


def read_chunk(directory: Path, record: dict) -> list[dict]:
    """The rows of one recorded chunk, after checking its SHA-256. / 核对 SHA-256 后返回一个已登记块的行。"""
    with open(Path(directory) / record["file"], "rb") as stream:
        stream.seek(record["offset"])
        member = stream.read(record["length"])
    if hashlib.sha256(member).hexdigest() != record["sha256"]:
        raise ValueError(f"chunk {record.get('chunk_id')} of {record['file']} does not match its digest")
    # Rows are separated by "\n" only; `splitlines` would also break on separators JSON leaves unescaped.
    # 行只以 "\n" 分隔；`splitlines` 还会在 JSON 未转义的分隔符处断开。
    return [json.loads(line) for line in gzip.decompress(member).decode("utf-8").split("\n") if line]


def verify(ledger, directory: Path) -> dict:
    """Every recorded chunk reads back with its digest, row count and id range. / 每个已登记块都能按摘要、行数与 id 范围读回。"""
    problems, rows = [], 0
    for record in ledger._rows("SELECT * FROM cap_event_archives ORDER BY chunk_id"):
        try:
            found = read_chunk(directory, record)
        except (OSError, ValueError) as error:
            problems.append(str(error)[:200])
            continue
        if len(found) != record["rows"] or found[0]["id"] != record["first_id"] or found[-1]["id"] != record["last_id"]:
            problems.append(f"chunk {record['chunk_id']} does not match its record")
        rows += len(found)
    return {"status": "passed" if not problems else "failed", "rows": rows, "problems": problems[:20]}
