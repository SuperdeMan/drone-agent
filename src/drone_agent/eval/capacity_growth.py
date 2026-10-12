"""Capacity growth ladder of the mission service (WP-P6-07, D085).

The resident desk's history grows by about two hundred missions and three hundred thousand accepted dock reports a
day. The ladder asks whether the service's own costs stay flat as that history grows. It synthesizes ledgers with 1x,
4x and 16x the desk's mission history (1291 missions on 2026-10-12) plus the dock status rows of the retention window,
then starts the formal service with the desk's four catalogs over the S0 logical world on each, and measures the
startup, the startup reconciliation, the status archive and a steady window of background passes and API calls, once
with the D085 capacity migration (`indexed`) and once without it (`plain`, the control). Nothing here is evidence about
PX4 or any robot.

Parts:
- `template`: fly the desk's workflow mix once in S0, one run at a time, and keep the case as the template.
- `generate`: clone the template's runs into a synthetic history. Every instance id is replaced, package hashes and
  journal hash chains are recomputed, evidence keeps its media, and dock status rows fill the window in time order.
- `check`: on a copy, the service reconciles a sample of the cloned missions without writing anything.
- `measure`: one (scale, variant) measurement, each in its own process.
- `inventory`: the distinct statements of a steady window with their query plans and the full scans among them.
- `judge`: the pre-registered criteria over the six measurements.

服务容量规模阶梯（WP-P6-07，D085）。常驻任务台的历史每天增长约两百个任务与三十万条被接受的机场报告。阶梯回答的问题是：
历史增长时服务自身的开销是否保持平稳。它合成任务历史为任务台 1 倍、4 倍、16 倍（2026-10-12 为 1291 个任务）并带保留窗口内
机场状态行的账本，在每个账本上以任务台的四个目录、S0 逻辑世界启动正式服务，测量启动、启动核对、状态归档，以及一段稳态窗口
内的后台处理与接口调用；带 D085 容量迁移（`indexed`）与不带（`plain`，对照）各测一次。这里的一切都不是关于 PX4 或任何机器人
的证据。

各部分：`template` 在 S0 中逐个飞一遍任务台的工作流组合，保留用例作为模板；`generate` 把模板中的运行克隆成合成历史：替换
全部实例 id，重算任务包哈希与执行日志哈希链，证据沿用其媒体，机场状态行按时间顺序填满窗口；`check` 在副本上让服务核对一批
克隆任务且不写入任何内容；`measure` 每个（规模，变体）测量在独立进程中进行；`inventory` 列出稳态窗口内的不同语句及其查询
计划与其中的全表扫描；`judge` 按预登记判据判定六次测量。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import random
import re
import shutil
import sqlite3
import statistics
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SEED = 20261012
# The resident desk on 2026-10-12 (read-only statistics): missions by project, the 30 unbound M2 missions counted as
# campus. / 2026-10-12 常驻任务台（只读统计）：按项目的任务数，30 个未绑定的 M2 任务计入园区。
DESK_MISSIONS = 1291
MIX = (("fleet", 740), ("vendor", 343), ("campus", 208))
STATUS_PER_DAY = 314_000  # accepted dock reports a day, mean of 2026-10-03..11 / 每天被接受的机场报告，10-03 至 10-11 均值
DESK_SPAN_DAYS = 19.0  # the desk's 1291 missions span 2026-09-23..10-11 / 任务台 1291 个任务分布在 9-23 至 10-11
RETENTION_DAYS = 7.0
STATUS_DAYS = 7.5  # the retention window plus half a day for the archive to move / 保留窗口另加半天供归档移出
DOCK_RATE = (("dock_s1", 1.0), ("dock_fa", 1.0), ("dock_fb", 1.0), ("dock_vd", 1.18))
SCALES = (1, 4, 16)
VARIANTS = ("indexed", "plain")
ORDER = tuple((scale, variant) for scale in SCALES for variant in VARIANTS)
STEADY_S = 600.0
RECONCILE_CAP_S = 900.0
LIVE_EVERY_S = 120.0
PROBE_EVERY_S = 1.0
REPORT_EVERY_S = 1.0
CYCLE_EVERY_S = 1.0
CHUNK_ROWS = 2000
# (kind, project, workflow, asset, review decision). The S0 campus analyzer marks its asset suspected; the reviewer
# dismisses it, so no clone carries an open finding or order. / （类型，项目，工作流，资产，复核决定）。S0 园区分析器把资产标为
# 疑似；复核人驳回，因此克隆不带未结的发现或工单。
TEMPLATE_RUNS = (
    ("fleet", "fleet_s0", "fleet_watch", "asset_red", "confirmed"),
    ("fleet", "fleet_s0", "fleet_watch", "asset_blue", "confirmed"),
    ("fleet", "fleet_s0", "fleet_watch", "road_north", "confirmed"),
    ("fleet", "fleet_s0", "fleet_watch", "asset_red_b", "confirmed"),
    ("fleet", "fleet_s0", "fleet_watch", "asset_blue_b", "confirmed"),
    ("fleet", "fleet_s0", "fleet_watch", "road_north_b", "confirmed"),
    ("vendor", "vendor_s3", "vendor_watch", "asset_blue", "confirmed"),
    ("vendor", "vendor_s3", "vendor_watch", "road_north", "confirmed"),
    ("campus", "campus_s1", "appearance_watch", "asset_blue", "dismissed"),
)
LIVE_ASSETS = ("asset_blue", "asset_red_b", "road_north", "asset_blue_b", "asset_red")
PROBE_PROJECTS = ("campus_s1", "fleet_s0", "vendor_s3")
API_METHODS = ("view", "list", "audit.list", "tasks.list", "workflows.list")
# Tables a run never owns: configuration, dock state and the archive manifest. / 运行从不拥有的表：配置、机场状态与归档清单。
SHARED_TABLES = frozenset({"meta", "robots", "sqlite_sequence", "op_catalogs", "wf_catalogs", "sc_catalogs",
                           "bz_catalogs", "op_dock_state", "op_dock_sessions", "op_dock_locks", "wf_schedules",
                           "cap_event_archives"})
DOCK_KINDS = frozenset({"status.accepted", "status.rejected", "session.new"})
# Snapshot references that name another activity's rows without belonging to it: a scheduler decision records the dock
# actions it saw. They are renamed in a clone but never join two runs. / 指向其他活动但不属于它的快照引用：调度决定记录了它看到
# 的机场动作。克隆时照样改名，但从不把两个运行连在一起。
CONTEXT_PREFIXES = {"sc_decisions": ("act-",)}
AUTO_KEYS = {"deliveries": "cursor", "op_events": "id", "issues": "id"}
# Instance ids: prefixed hex ids, vendor UUIDs, capture evidence ids and bare 32-hex delivery and command ids. Worker
# ids (wfw-, bzw-, sch-) and the service's deployment run id (service-…) are shared, so they are not instance ids.
# 实例 id：带前缀的十六进制 id、厂商 UUID、拍摄证据 id 与 32 位十六进制的投递与命令 id。工作进程 id（wfw-、bzw-、sch-）与服务
# 部署运行 id（service-…）是共享的，不算实例 id。
TOKEN = re.compile(
    r"(?<![0-9A-Za-z_])(?:(?:m|req|job|fd|ord|act|res|dec|tk|sd|wr|ob|an|rv|wo|fb|start)-[0-9a-f]{6,32}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|capture:[0-9a-f]{64})(?![0-9a-f])"
    r"|(?<![0-9A-Za-z_-])[0-9a-f]{32}(?![0-9A-Za-z_])")
HEX64 = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def code_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def bench() -> float:
    """Milliseconds of a fixed CPU-bound task, to show the host did not throttle a run (D084 lesson).

    一个固定 CPU 任务的毫秒数，用来表明主机没有对运行降速（D084 教训）。
    """
    started = time.perf_counter()
    block = b"\x5a" * (1 << 20)
    digest = hashlib.sha256()
    for _ in range(96):
        digest.update(block)
    total = 0
    for value in range(1_500_000):
        total += value * value % 7
    return round((time.perf_counter() - started) * 1000, 1)


def percentiles(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)

    def pick(q: float) -> float:
        return round(ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)], 3)

    return {"n": len(ordered), "mean": round(statistics.fmean(ordered), 3), "p50": pick(0.5), "p95": pick(0.95),
            "p99": pick(0.99), "max": round(ordered[-1], 3)}


# ── template / 模板 ──

async def make_template(case: Path, *, seed: int) -> dict:
    """Fly the template runs one at a time and check each ends completed with one mission and nothing left open.

    逐个飞模板运行，并核对每个都以完成结束、只有一个任务、没有遗留未结事项。
    """
    from drone_agent.eval.p5_desk_world import P6DeskWorld

    # The template is the ledger before D085: the `indexed` variant migrates it, the `plain` one never does.
    # 模板是 D085 之前的账本：`indexed` 变体对其迁移，`plain` 变体从不迁移。
    _skip_capacity_migration()
    world = P6DeskWorld(case, ROOT, seed=seed)
    runs = []
    try:
        await world.settle()
        for kind, project, workflow, asset, decision in TEMPLATE_RUNS:
            run = await world.start(workflow, {"asset": asset}, project=project,
                                    request_id=f"start-{uuid.uuid4().hex[:16]}")
            await world.run_business([run], until=lambda: world.final(run), what=f"template {workflow} {asset}",
                                     decisions=lambda run_id, node_id: decision, timeout_s=300)
            missions = [m["mission_id"] for m in world.missions_of(run)]
            runs.append({"kind": kind, "project_id": project, "workflow_id": workflow, "asset": asset,
                         "decision": decision, "run_id": run,
                         "state": world.run_state(run), "missions": missions,
                         "mission_status": [world.ledger.mission(m)["status"] for m in missions]})
        for _ in range(30):
            await world.step()
        open_findings = world.ledger._rows("SELECT finding_id FROM bz_findings WHERE state IN ('candidate', 'confirmed')")
        open_orders = world.ledger._rows("SELECT order_id FROM bz_orders WHERE state != 'closed'")
    finally:
        world.service.business.jobs.stop()
        world.ledger.close()
    problems = [f"{r['run_id']}: {r['state']} {r['mission_status']}" for r in runs
                if r["state"] != "completed" or r["mission_status"] != ["completed"]]
    if open_findings or open_orders:
        problems.append(f"open findings {len(open_findings)}, open orders {len(open_orders)}")
    if problems:
        raise RuntimeError(f"the template is not clean: {problems}")
    ledger = case / "service/ledger.sqlite3"
    if _rows(ledger, "SELECT name FROM sqlite_master WHERE name LIKE 'cap_%'"):
        raise RuntimeError("the template must not carry the D085 objects")
    finished = datetime.fromisoformat(max(row["created_at"] for row in _rows(ledger, "SELECT created_at FROM op_events")))
    return {"format": "drone.capacity-template/v1", "seed": seed, "runs": runs,
            "finished_at": finished.replace(microsecond=0).isoformat(),
            "ledger_sha256": sha256_file(case / "service/ledger.sqlite3")}


def _rows(path: Path, sql: str, args=()) -> list[dict]:
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute(sql, args)]
    finally:
        db.close()


# ── clone / 克隆 ──

# Whole-second wall-clock stamps; fractions and zones are left as written. / 整秒的时间戳部分；小数与时区原样保留。
STAMP = re.compile(r"(?<![0-9])(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?=(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?(?![0-9]))")


def _tables(db: sqlite3.Connection) -> list[str]:
    return [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def _shift(value: str, seconds: int) -> str:
    if not seconds or "T" not in value:
        return value
    return STAMP.sub(lambda m: (datetime.fromisoformat(m.group(1)) + timedelta(seconds=seconds))
                     .strftime("%Y-%m-%dT%H:%M:%S"), value)


def _earliest(rows) -> datetime:
    return min(datetime.fromisoformat(m.group(1)) for _, _, row in rows for value in row.values()
               if isinstance(value, str) for m in STAMP.finditer(value))


def extract(db: sqlite3.Connection, run_ids: list[str]) -> tuple[dict[str, list[tuple[str, int, dict]]],
                                                                dict[str, int]]:
    """Each run's bundle of (table, rowid, row): the rows its instance ids reach, closed under the ids those rows carry;
    and the counts of rows no run owns. A row reached from two runs is an error.

    每个运行的（表，rowid，行）包：其实例 id 能到达的行，并按这些行所带 id 求闭包；以及不属于任何运行的行数。被两个运行到达
    的行视为错误。
    """
    db.row_factory = sqlite3.Row
    rows: list[tuple[str, int, dict, frozenset]] = []
    for table in _tables(db):
        if table in SHARED_TABLES:
            continue
        for row in db.execute(f'SELECT rowid AS "__rowid__", * FROM "{table}" ORDER BY rowid'):
            record = dict(row)
            rowid = record.pop("__rowid__")
            if table == "op_events" and record["kind"] in DOCK_KINDS:
                continue
            context = CONTEXT_PREFIXES.get(table, ())
            tokens = frozenset(m.group(0) for value in record.values() if isinstance(value, str)
                               for m in TOKEN.finditer(value) if not m.group(0).startswith(context))
            rows.append((table, rowid, record, tokens))
    owner: dict[int, str] = {}
    bundles: dict[str, list[tuple[str, int, dict]]] = {}
    for run in run_ids:
        reached, members, changed = {run}, set(), True
        while changed:
            changed = False
            for index, (_, _, _, tokens) in enumerate(rows):
                if index not in members and tokens & reached:
                    members.add(index)
                    reached |= tokens
                    changed = True
        for index in members:
            if index in owner:
                raise ValueError(f"{rows[index][0]} row reached from {owner[index]} and {run}")
            owner[index] = run
        bundles[run] = [rows[i][:3] for i in sorted(members)]
        if sum(table == "missions" for table, _, _ in bundles[run]) != 1:
            raise ValueError(f"{run} does not own exactly one mission")
    unowned: dict[str, int] = {}
    for index, (table, _, _, _) in enumerate(rows):
        if index not in owner:
            unowned[table] = unowned.get(table, 0) + 1
    return bundles, unowned


class Renamer:
    """Fresh instance ids of the same shape, one per original id within a clone. / 同形的新实例 id，克隆内一一对应。"""

    def __init__(self, rng: random.Random, used: set[str]):
        self.rng, self.used, self.map = rng, used, {}

    def _hex(self, length: int) -> str:
        return f"{self.rng.getrandbits(4 * length):0{length}x}"

    def _fresh(self, old: str) -> str:
        if old.startswith("capture:"):
            return "capture:" + self._hex(64)
        if old.count("-") == 4:
            return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))
        if "-" in old:
            prefix, digits = old.split("-", 1)
            return f"{prefix}-{self._hex(len(digits))}"
        return self._hex(len(old))

    def __call__(self, match: re.Match) -> str:
        old = match.group(0)
        new = self.map.get(old)
        if new is None:
            new = self._fresh(old)
            while new in self.used:
                new = self._fresh(old)
            self.used.add(new)
            self.map[old] = new
        return new


def _replace_hashes(rows: list[tuple[str, dict]], mapping: dict[str, str]) -> None:
    if not mapping:
        return
    for _, row in rows:
        for key, value in row.items():
            if isinstance(value, str) and len(value) >= 64:
                row[key] = HEX64.sub(lambda m: mapping.get(m.group(0), m.group(0)), value)


def transform(bundle: list[tuple[str, int, dict]], renamer: Renamer | None, seconds: int) -> list[tuple[str, dict]]:
    """A run bundle with new instance ids (none when `renamer` is None) and wall-clock stamps moved by `seconds`, its
    derived digests recomputed as the service computes them: package hashes, sealed sources, journal hash chains and
    event ids, and the report's provenance export.

    运行包换上新实例 id（`renamer` 为 None 时不换）、时间戳平移 `seconds` 秒，并按服务的算法重算派生摘要：任务包哈希、封印
    的来源、执行日志哈希链与事件 id，以及报告的溯源导出。
    """
    from drone_agent.contracts.mission import MissionPackage
    from drone_agent.fleet.events import event_to_row
    from drone_agent.fleet.ledger import BusinessLedger
    from drone_agent.fleet.provenance import NAMESPACE, digest, export
    from drone_agent.runtime.ledger import content_hash

    rows = [(table, {key: _shift(TOKEN.sub(renamer, value) if renamer else value, seconds)
                     if isinstance(value, str) else value for key, value in row.items()}) for table, _, row in bundle]
    packages = {}
    for table, row in rows:
        if table == "versions" and row.get("package"):
            package = MissionPackage.model_validate_json(row["package"])
            packages[row["package_hash"]] = package.compute_hash()
    _replace_hashes(rows, packages)
    # Saved sources are sealed with the digest of their body. / 已保存的来源以其内容摘要封印。
    seals = {}
    for table, row in rows:
        if table == "versions" and row.get("decision"):
            decision = json.loads(row["decision"])
            for record in (decision.get(NAMESPACE) or {}).values():
                if isinstance(record, dict) and set(record) == {"body", "sha256"}:
                    fresh = digest(record["body"])
                    seals[record["sha256"]], record["sha256"] = fresh, fresh
            row["decision"] = json.dumps(decision, ensure_ascii=False, sort_keys=True)
    _replace_hashes([(table, row) for table, row in rows if table != "versions"], seals)
    chains, ids, journals = {}, {}, {}
    for table, row in rows:
        if table == "events":
            journals.setdefault((row["mission_id"], row["mission_version"], row["journal"]), []).append(row)
    for (mission_id, version, journal), events in journals.items():
        previous = "0" * 64
        for row in sorted(events, key=lambda r: int(r["seq"])):
            body = json.loads(row["body"])
            body["data"]["previous"] = previous
            rebuilt = event_to_row(body)
            sha = content_hash({key: value for key, value in rebuilt.items() if key != "sha256"})
            body["data"]["sha256"] = sha
            new_id = f"{row['robot_id']}:{mission_id}:v{version}:{journal}:{row['seq']}:{sha[:16]}"
            chains[row["sha256"]], ids[row["event_id"]] = sha, new_id
            body["event_id"] = new_id
            row.update(event_id=new_id, sha256=sha, previous=previous,
                       body=json.dumps(body, ensure_ascii=False, sort_keys=True))
            previous = sha
    # A journal row naming another row's hash would need that hash first; the desk's journals never do.
    # 若日志行引用另一行的哈希，就要先算出那个哈希；任务台的日志从不如此。
    stale = set(chains) - set(chains.values())
    if any(m.group(0) in stale for table, row in rows if table == "events" for m in HEX64.finditer(row["body"])):
        raise ValueError("a journal row refers to another row's hash")
    _replace_hashes([(table, row) for table, row in rows if table != "events"], chains)
    if ids:
        pattern = re.compile("|".join(re.escape(old) for old in sorted(ids, key=len, reverse=True)))
        for table, row in rows:
            for key, value in row.items():
                if isinstance(value, str) and ":v" in value and (table != "events" or key == "body"):
                    row[key] = pattern.sub(lambda m: ids[m.group(0)], value)
    # The report carries each version's provenance export with its reproducible digest; rebuild it as the service
    # does. / 报告带有每个版本的溯源导出及其可复算摘要；按服务的方式重建。
    versions = sorted((BusinessLedger._decode_version(dict(row)) for table, row in rows if table == "versions"),
                      key=lambda record: int(record["version"]))
    for table, row in rows:
        if table == "reports":
            report = json.loads(row["body"])
            report["provenance"] = [export(record) for record in versions]
            row["body"] = json.dumps(report, ensure_ascii=False, sort_keys=True)
    return rows


def _proportions(total: int) -> dict[str, int]:
    """Largest-remainder split of `total` clones by the desk's mix. / 按任务台构成以最大余数法分配。"""
    weight = sum(count for _, count in MIX)
    exact = {kind: total * count / weight for kind, count in MIX}
    found = {kind: int(value) for kind, value in exact.items()}
    for kind, _ in sorted(exact.items(), key=lambda item: item[1] - int(item[1]), reverse=True)[:total - sum(
            found.values())]:
        found[kind] += 1
    return found


def _status_rows(template_db: sqlite3.Connection, end: datetime):
    """Accepted dock reports for STATUS_DAYS before `end` as (time, row), in time order, at the desk's per-dock rates.

    `end` 之前 STATUS_DAYS 天、按任务台各机场频率、按时间顺序的被接受机场报告，形如（时刻，行）。
    """
    latest = {}
    for row in template_db.execute("SELECT subject, actor, body FROM op_events WHERE kind='status.accepted' "
                                   "ORDER BY id"):
        latest[row[0]] = (row[1], json.loads(row[2])["boot_id"])
    total_rate = sum(rate for _, rate in DOCK_RATE)
    count = round(STATUS_DAYS * STATUS_PER_DAY)
    step = STATUS_DAYS * 86400 / count
    start = end - timedelta(days=STATUS_DAYS)
    rng = random.Random(SEED)
    docks = [dock for dock, _ in DOCK_RATE]
    weights = [rate / total_rate for _, rate in DOCK_RATE]
    seq = {dock: 0 for dock in docks}
    for index in range(count):
        dock = rng.choices(docks, weights)[0]
        seq[dock] += 1
        actor, boot = latest[f"dock:{dock}"]
        moment = start + timedelta(seconds=index * step)
        at = moment.isoformat()
        yield moment, (f"dock:{dock}", "status.accepted", actor,
                       json.dumps({"boot_id": boot, "finished": [], "locked": None, "observed_at": at,
                                   "seq": seq[dock], "session": "active"}, ensure_ascii=False, sort_keys=True), at)


def generate(template: Path, out: Path, *, scale: int, seed: int = SEED) -> dict:
    """A case whose ledger holds `scale` times the desk's missions over `scale` times its span, in time order: the
    template's own rows open the history, the clones follow in evenly spaced slots, and the dock status rows of the
    last STATUS_DAYS interleave with them by time.

    生成一个用例：其账本在任务台 `scale` 倍的时间跨度内按时间顺序含 `scale` 倍的任务。模板自身的行位于历史开头，克隆按等距
    时间槽依次排列，最后 STATUS_DAYS 天的机场状态行按时间与它们交错。
    """
    started = time.perf_counter()
    manifest = json.loads((template / "template.json").read_text(encoding="utf-8"))
    if out.exists():
        raise FileExistsError(out)
    shutil.copytree(template / "case", out / "case")
    ledger = out / "case/service/ledger.sqlite3"
    source = sqlite3.connect(f"file:{(template / 'case/service/ledger.sqlite3').as_posix()}?mode=ro", uri=True)
    runs = {run["run_id"]: run["kind"] for run in manifest["runs"]}
    bundles, unowned = extract(source, list(runs))
    by_kind: dict[str, list[list[tuple[str, int, dict]]]] = {}
    for run, kind in runs.items():
        by_kind.setdefault(kind, []).append(bundles[run])
    used = {m.group(0) for bundle in bundles.values() for _, _, row in bundle for value in row.values()
            if isinstance(value, str) for m in TOKEN.finditer(value)}
    template_missions = sum(len(run["missions"]) for run in manifest["runs"])
    clones = round(scale * DESK_MISSIONS) - template_missions
    rng = random.Random(seed * 1000 + scale)
    order = [kind for kind, count in _proportions(clones).items() for _ in range(count)]
    rng.shuffle(order)
    end = datetime.fromisoformat(manifest["finished_at"])
    begin = end - timedelta(days=DESK_SPAN_DAYS * scale)
    first = min(datetime.fromisoformat(m.group(1)) for (value,) in source.execute(
        "SELECT created_at FROM op_events UNION ALL SELECT created_at FROM missions") for m in STAMP.finditer(value))
    template_shift = int((begin.replace(tzinfo=None) - first).total_seconds())
    status = _status_rows(source, end)
    status_total = round(STATUS_DAYS * STATUS_PER_DAY)
    db = sqlite3.connect(str(ledger), isolation_level=None)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    columns = {table: [r[1] for r in db.execute(f'PRAGMA table_info("{table}")') if r[1] != AUTO_KEYS.get(table)]
               for table in _tables(db)}
    statements = {table: f'INSERT INTO "{table}" ({", ".join(columns[table])}) VALUES '
                         f'({", ".join("?" * len(columns[table]))})' for table in columns}
    status_sql = "INSERT INTO op_events (subject, kind, actor, body, created_at) VALUES (?,?,?,?,?)"
    turn = {kind: 0 for kind in by_kind}
    written = status_written = 0
    pending = next(status, None)
    db.execute("BEGIN")
    try:
        # The template's own rows move to the beginning of the history, ids kept. / 模板自身的行移到历史开头，保留 id。
        owned = {(table, rowid) for bundle in bundles.values() for table, rowid, _ in bundle}
        for bundle in bundles.values():
            for (table, rowid, _), (_, row) in zip(bundle, transform(bundle, None, template_shift), strict=True):
                db.execute(f'UPDATE "{table}" SET {", ".join(f"{c}=?" for c in columns[table])} WHERE rowid=?',
                           [row[c] for c in columns[table]] + [rowid])
        for table in _tables(db):
            texts = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")') if r[2].upper() == "TEXT"]
            for row in db.execute(f'SELECT rowid, {", ".join(texts)} FROM "{table}"').fetchall() if texts else []:
                if (table, row[0]) in owned:
                    continue
                moved = [_shift(value, template_shift) if isinstance(value, str) else value for value in row[1:]]
                if moved != list(row[1:]):
                    db.execute(f'UPDATE "{table}" SET {", ".join(f"{c}=?" for c in texts)} WHERE rowid=?',
                               [*moved, row[0]])
        slot_span = timedelta(days=DESK_SPAN_DAYS * scale) - timedelta(hours=2)
        for index, kind in enumerate(order):
            bundle = by_kind[kind][turn[kind] % len(by_kind[kind])]
            turn[kind] += 1
            slot = begin + timedelta(hours=1) + slot_span * index / len(order)
            batch = []
            while pending is not None and pending[0] < slot:
                batch.append(pending[1])
                pending = next(status, None)
            db.executemany(status_sql, batch)
            status_written += len(batch)
            shift = int((slot.replace(tzinfo=None) - _earliest(bundle)).total_seconds())
            for table, row in transform(bundle, Renamer(rng, used), shift):
                db.execute(statements[table], [row[column] for column in columns[table]])
                written += 1
            if index % 500 == 499:
                db.execute("COMMIT")
                db.execute("BEGIN")
        rest = ([pending[1]] if pending is not None else []) + [row for _, row in status]
        db.executemany(status_sql, rest)
        status_written += len(rest)
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    problems = db.execute("PRAGMA foreign_key_check").fetchall()
    db.execute("PRAGMA journal_mode=WAL")
    counts = {table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in _tables(db)}
    db.close()
    source.close()
    if problems:
        raise RuntimeError(f"foreign key problems: {problems[:5]}")
    if status_written != status_total:
        raise RuntimeError(f"wrote {status_written} of {status_total} status rows")
    return {"format": "drone.capacity-generated/v1", "scale": scale, "seed": seed, "clones": len(order),
            "clones_by_kind": _proportions(clones), "template_sha256": manifest["ledger_sha256"],
            "template_missions": [m for run in manifest["runs"] for m in run["missions"]],
            "code_sha256": code_sha256(), "history_start": begin.isoformat(), "template_shift_s": template_shift,
            "status_rows": status_total, "status_start": (end - timedelta(days=STATUS_DAYS)).isoformat(),
            "end": end.isoformat(), "rows_cloned": written, "unowned_template_rows": unowned, "counts": counts,
            "ledger_bytes": ledger.stat().st_size, "ledger_sha256": sha256_file(ledger),
            "elapsed_s": round(time.perf_counter() - started, 1)}


# ── the measurement world / 测量世界 ──

def _measure_world_class():
    from drone_agent.contracts import utcnow
    from drone_agent.eval.p5_desk_world import P6DeskWorld

    class MeasureWorld(P6DeskWorld):
        """The desk platform over the S0 world at the desk's cadence: docks report and aircraft sync once a second;
        the service runs its own background loop instead of being stepped.

        以任务台节奏运行的 S0 任务台平台：机场每秒报告一次、飞行器每秒同步一次；服务运行自己的后台循环而不是被步进。
        """

        last_cycle = 0.0

        def __init__(self, case: Path, repo: Path, *, seed: int):
            super().__init__(case, repo, seed=seed)
            # The history's packages were flown before this process started. / 历史任务包在本进程启动前已飞过。
            for uav in self.uavs.values():
                history = uav.dir / "inbox/history"
                if history.is_dir():
                    uav.flown.update(path.name for path in history.glob("*.json"))

        async def advance(self) -> None:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            elapsed, self.last_step = now - self.last_step, now
            for dock in self.docks.values():
                dock.advance(elapsed)
            wall = utcnow()
            for backend in self.backends:
                due = {d: s for d, s in backend.docks.items() if now - self.last_report.get(d, 0.0) >= REPORT_EVERY_S}
                if due:
                    for dock_id in due:
                        self.last_report[dock_id] = now
                    keep, backend.docks = backend.docks, due
                    try:
                        await backend.cycle(wall)
                    finally:
                        backend.docks = keep
            if now - self.last_cycle >= CYCLE_EVERY_S:
                self.last_cycle = now
                for uav in self.uavs.values():
                    await uav.cycle()
            self.vendor_sim.step(now - self.vendor_last)
            self.vendor_last = now

    return MeasureWorld


def _skip_capacity_migration() -> None:
    """The control: build the service without the D085 migration. / 对照：不做 D085 迁移构建服务。"""
    import drone_agent.fleet.capacity_store as capacity_store

    capacity_store.migrate = lambda ledger, *, backups, clock=None: {"status": "skipped"}


class Recorder:
    """Every recorded duration of a phase, kept in full (no window). / 一个阶段记录的全部耗时（无窗口）。"""

    def __init__(self):
        self.values: dict[str, list[float]] = {}

    def record(self, name: str, milliseconds: float) -> None:
        self.values.setdefault(name, []).append(milliseconds)

    def summary(self) -> dict:
        return {name: percentiles(values) for name, values in sorted(self.values.items())}


async def _measure(case: Path, *, scale: int, variant: str, generated: dict, inventory: bool = False,
                   steady_s: float = STEADY_S) -> dict:
    from drone_agent.eval.p5_world import ADMIN, OPERATOR, VIEWER
    from drone_agent.fleet.api import dispatch
    from drone_agent.fleet.archive import KIND, StatusArchive, verify

    result: dict = {"scale": scale, "variant": variant, "bench_ms_before": bench()}
    ledger_path = case / "service/ledger.sqlite3"
    if variant == "indexed":
        from drone_agent.fleet.capacity_store import migrate
        from drone_agent.fleet.ledger import BusinessLedger

        started = time.perf_counter()
        ledger = BusinessLedger(ledger_path)
        try:
            report = migrate(ledger, backups=case / "service/backups")
        finally:
            ledger.close()
        result["migration"] = {"status": report["status"], "s": round(time.perf_counter() - started, 2),
                               "backup_bytes": sum(p.stat().st_size for p in (case / "service/backups").glob("*"))}
        for path in (case / "service/backups").glob("ledger-v2-cap-*"):
            path.unlink()  # the measurement needs no rollback copy / 测量不需要回退副本
    else:
        _skip_capacity_migration()
    # Read before the clock starts: the history to probe and the status rows to conserve.
    # 计时开始前读取：要探测的历史与要守恒的状态行。
    history = [row["mission_id"] for row in _rows(ledger_path, "SELECT mission_id FROM missions")]
    status_before = _rows(ledger_path, "SELECT COUNT(*) AS n, MAX(id) AS top FROM op_events WHERE kind=?", (KIND,))[0]
    issues_before = _rows(ledger_path, "SELECT COUNT(*) AS n FROM issues")[0]["n"]
    phases = {"startup": Recorder()}
    refreshes: list[float] = []
    pass_ends: list[float] = []
    t0 = time.perf_counter()
    world = _measure_world_class()(case, ROOT, seed=7)
    service = world.service
    result["construct_s"] = round(time.perf_counter() - t0, 3)
    current = {"recorder": phases["startup"]}
    capacity = service.capacity
    capacity_record = capacity.record

    def record(name: str, milliseconds: float) -> None:
        capacity_record(name, milliseconds)
        current["recorder"].record(name, milliseconds)

    capacity.record = record
    original_refresh, original_pass = service.refresh, service._pass

    def refresh(mission_id: str):
        started = time.perf_counter()
        try:
            return original_refresh(mission_id)
        finally:
            refreshes.append((time.perf_counter() - started) * 1000)

    async def timed_pass(stop):
        await original_pass(stop)
        pass_ends.append(time.perf_counter())

    service.refresh, service._pass = refresh, timed_pass
    statements: dict[str, list] = {}

    def trace(sql: str) -> None:
        entry = statements.setdefault(_normal(sql), [sql, 0])
        entry[1] += 1

    rng = random.Random(SEED + scale)
    stop = asyncio.Event()
    live_runs: list[str] = []

    async def advance_loop():
        while not stop.is_set():
            await world.advance()

    async def probe_loop():
        turn = 0
        while not stop.is_set():
            await asyncio.sleep(PROBE_EVERY_S)
            method = API_METHODS[turn % len(API_METHODS)]
            project = PROBE_PROJECTS[(turn // len(API_METHODS)) % len(PROBE_PROJECTS)]
            turn += 1
            params = {"view": {"mission_id": rng.choice(history)}, "list": {},
                      "audit.list": {"project_id": project, "before": None, "limit": 50},
                      "tasks.list": {"project_id": "fleet_s0"}, "workflows.list": {"project_id": project}}[method]
            response = await dispatch(service, {"method": method, "actor": VIEWER, "trust": "first_party",
                                                "params": params})
            if not response.get("ok"):
                result.setdefault("probe_errors", []).append({"method": method, "issue": response.get("issue")})

    async def live_loop():
        started = time.monotonic()
        while not stop.is_set():
            if len(live_runs) < int((time.monotonic() - started) // LIVE_EVERY_S) + 1:
                asset = LIVE_ASSETS[len(live_runs) % len(LIVE_ASSETS)]
                live_runs.append(await world.start("fleet_watch", {"asset": asset}, project="fleet_s0",
                                                   actor=OPERATOR, request_id=f"start-{uuid.uuid4().hex[:16]}"))
            for run in live_runs:
                if not world.final(run):
                    await world.approve_waiting(run)
            await asyncio.sleep(1.0)

    tasks = [asyncio.create_task(service.run(stop)), asyncio.create_task(advance_loop()),
             asyncio.create_task(probe_loop())]
    try:
        while not pass_ends:
            await asyncio.sleep(0.05)
        result["first_pass_s"] = round(pass_ends[0] - t0, 3)
        total = len(service.backlog)
        result["reconcile"] = {"missions": total}
        started = time.perf_counter()
        while service.backlog and time.perf_counter() - started < RECONCILE_CAP_S:
            await asyncio.sleep(0.5)
        left = len(service.backlog)
        reconciled = list(refreshes)
        result["reconcile"].update(wall_s=round(time.perf_counter() - started, 1), left=left, capped=left > 0,
                                   refresh_ms=percentiles(reconciled))
        if left:
            service.backlog.clear()  # the control stops here; the steady window follows / 对照到此为止，随后是稳态窗口
        phases["archive"] = Recorder()
        current["recorder"] = phases["archive"]
        if variant == "indexed":
            archive = StatusArchive(world.ledger, case / "service/archive/op_events", retention_days=RETENTION_DAYS,
                                    clock=lambda: datetime.fromisoformat(generated["end"]))
            due = archive.due()
            service.status_archive = archive
            started = time.perf_counter()
            while True:
                archived = world.ledger._one("SELECT COALESCE(SUM(rows), 0) AS n FROM cap_event_archives")["n"]
                if due - archived < CHUNK_ROWS or time.perf_counter() - started > 1800:
                    break
                await asyncio.sleep(1.0)
            result["archive"] = {"due": due, "archived": archived, "wall_s": round(time.perf_counter() - started, 1),
                                 "chunks": world.ledger._one("SELECT COUNT(*) AS n FROM cap_event_archives")["n"]}
        phases["steady"] = Recorder()
        current["recorder"] = phases["steady"]
        if inventory:
            world.ledger._db.set_trace_callback(trace)
        tasks.append(asyncio.create_task(live_loop()))
        steady_started = time.perf_counter()
        await asyncio.sleep(steady_s)
        result["steady_s"] = round(time.perf_counter() - steady_started, 1)
        if inventory:
            world.ledger._db.set_trace_callback(None)
        capacity_report = await dispatch(service, {"method": "capacity.get", "actor": ADMIN, "trust": "first_party",
                                                   "params": {"project_id": "fleet_s0"}})
        result["capacity_get_ok"] = bool(capacity_report.get("ok"))
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        service.business.jobs.stop()
    result["live_runs"] = {"started": len(live_runs), "completed": sum(world.final(r) for r in live_runs)}
    result["phases"] = {name: recorder.summary() for name, recorder in phases.items()}
    result["issues_added"] = [dict(row) for row in world.ledger._rows(
        "SELECT code, body FROM issues ORDER BY id LIMIT -1 OFFSET ?", (issues_before,))][:20]
    if variant == "indexed":
        after = world.ledger._one("SELECT COUNT(*) AS n FROM op_events WHERE kind=? AND id <= ?",
                                  (KIND, status_before["top"]))["n"]
        archived = world.ledger._one("SELECT COALESCE(SUM(rows), 0) AS n FROM cap_event_archives")["n"]
        result["archive"].update(conserved=after + archived == status_before["n"],
                                 verify=verify(world.ledger, case / "service/archive/op_events"))
    if inventory:
        result["statements"] = _plans(world.ledger, statements)
    world.ledger.close()
    result["bench_ms_after"] = bench()
    return result


_LITERAL = re.compile(r"'(?:[^']|'')*'|\b\d+(?:\.\d+)?\b")


def _normal(sql: str) -> str:
    return _LITERAL.sub("?", " ".join(sql.split()))


GROWING = frozenset({"missions", "requests", "versions", "events", "deliveries", "evidence", "verifications", "facts",
                     "issues", "operations", "reports", "op_bindings", "op_reservations", "op_holds", "op_claims",
                     "op_decisions", "op_dock_actions", "op_events", "op_cancellations", "wf_runs", "wf_nodes",
                     "wf_outbox", "wf_triggers", "wf_reviews", "wf_analyses", "wf_work_orders", "sc_tasks",
                     "sc_assignments", "sc_decisions", "sc_footprints", "bz_jobs", "bz_findings", "bz_orders",
                     "bz_rounds", "bz_reviews", "bz_references", "cap_event_archives"})


def _plans(ledger, statements: dict[str, list]) -> list[dict]:
    """Each distinct statement with its count and plan; full scans of growing tables flagged.

    每条不同语句及其次数与计划；标出对增长表的全表扫描。
    """
    found = []
    for normal, (example, count) in sorted(statements.items(), key=lambda item: -item[1][1]):
        if not normal.upper().startswith(("SELECT", "WITH", "UPDATE", "DELETE")):
            continue
        try:
            # Unexpanded statements keep their placeholders; NULLs give the same plan. / 未展开的语句保留占位符；绑定 NULL 计划相同。
            plan = [row[3] for row in ledger._db.execute("EXPLAIN QUERY PLAN " + example,
                                                          [None] * example.count("?"))]
        except sqlite3.Error as error:
            plan = [f"error: {error}"]
        scans = sorted({m.group(1) for line in plan for m in [re.match(r"\s*SCAN (\w+)", line)]
                        if m and m.group(1) in GROWING})
        found.append({"sql": normal[:400], "count": count, "plan": plan, "scans": scans})
    return found


# ── judge / 判定 ──

def judge(results: list[dict], prereg: dict) -> dict:
    """The pre-registered criteria (docs/capacity-retention-design.md §6) over the six measurements.

    按预登记判据（docs/capacity-retention-design.md §6）判定六次测量。
    """
    found = {(r["scale"], r["variant"]): r for r in results}
    problems = [f"missing {scale}x {variant}" for scale, variant in ORDER if (scale, variant) not in found]
    if problems:
        return {"status": "invalid", "problems": problems}
    benches = [value for r in results for value in (r["bench_ms_before"], r["bench_ms_after"])]
    middle = statistics.median(benches)
    validity = {
        "inputs": all(r["generated"]["template_sha256"] == prereg["template_sha256"]
                      and r["generated"]["code_sha256"] == prereg["code_sha256"]
                      and r.get("code_sha256") == prereg["code_sha256"] for r in results),
        "no_throttling": all(middle / 1.5 <= value <= middle * 1.5 for value in benches),
        "probes_answered": all(not r.get("probe_errors") for r in results),
        "full_windows": all(r["steady_s"] >= STEADY_S - 5 for r in results),
    }
    one, top = found[(1, "indexed")], found[(16, "indexed")]
    indexed = [found[(scale, "indexed")] for scale in SCALES]

    def steady(r: dict, name: str, q: str) -> float:
        return r["phases"]["steady"].get(name, {}).get(q, float("inf"))

    criteria = {
        "pass_p95": steady(top, "pass", "p95") <= max(1.5 * steady(one, "pass", "p95"), steady(one, "pass", "p95") + 10),
        "api_p95": all(steady(top, f"api.{m}", "p95") <= min(200.0, max(2 * steady(one, f"api.{m}", "p95"),
                                                                         steady(one, f"api.{m}", "p95") + 10))
                       for m in API_METHODS),
        "reconcile_per_mission": not top["reconcile"]["capped"] and top["reconcile"]["refresh_ms"]["mean"] <= max(
            1.5 * one["reconcile"]["refresh_ms"]["mean"], one["reconcile"]["refresh_ms"]["mean"] + 1.0),
        "dock_report_p99": all(steady(r, "api.docks.report", "p99") <= 100.0 for r in indexed),
        "archive": all(r["archive"]["conserved"] and r["archive"]["verify"]["status"] == "passed"
                       and r["archive"]["due"] - r["archive"]["archived"] < CHUNK_ROWS
                       and r["phases"]["archive"].get("pass", {}).get("p99", float("inf")) <= 100.0 for r in indexed),
        "no_issues": all(not r["issues_added"] for r in indexed),
    }
    valid = all(validity.values())
    return {"status": "invalid" if not valid else "passed" if all(criteria.values()) else "failed",
            "validity": validity, "criteria": criteria}


# ── entry / 入口 ──

def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8",
                    newline="\n")


async def _check(case: Path, generated: dict, sample: int) -> dict:
    """The service reconciles sampled clones and every template mission without writing. / 核对抽样克隆与模板任务且不写入。"""
    _skip_capacity_migration()
    world = _measure_world_class()(case, ROOT, seed=7)
    try:
        template = generated["template_missions"]
        clones = [row["mission_id"] for row in world.ledger._rows("SELECT mission_id FROM missions ORDER BY rowid")
                  if row["mission_id"] not in template]
        chosen = template + random.Random(SEED).sample(clones, min(sample, len(clones)))
        writes = {}
        for mission_id in chosen:
            before = world.ledger._db.total_changes
            world.service.refresh(mission_id)
            await world.service.enrich(mission_id)
            if world.ledger._db.total_changes != before:
                writes[mission_id] = world.ledger._db.total_changes - before
        statuses = {row["status"]: row["n"] for row in world.ledger._rows(
            "SELECT status, COUNT(*) AS n FROM missions GROUP BY status")}
    finally:
        world.service.business.jobs.stop()
        world.ledger.close()
    return {"status": "passed" if not writes else "failed", "checked": len(chosen), "writes": writes,
            "statuses": statuses}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("part", choices=("template", "generate", "check", "measure", "inventory", "judge"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--scale", type=int, choices=SCALES)
    parser.add_argument("--variant", choices=VARIANTS)
    parser.add_argument("--sample", type=int, default=200)
    parser.add_argument("--prereg", type=Path)
    parser.add_argument("--steady-s", type=float, default=STEADY_S)
    args = parser.parse_args()
    out = args.output
    if args.part == "template":
        manifest = asyncio.run(make_template(out / "case", seed=SEED))
        manifest["code_sha256"] = code_sha256()
        _write(out / "template.json", manifest)
        print(json.dumps({"status": "ok", "ledger_sha256": manifest["ledger_sha256"]}))
    elif args.part == "generate":
        generated = generate(args.template, out / f"x{args.scale}", scale=args.scale)
        _write(out / f"x{args.scale}" / "generated.json", generated)
        print(json.dumps({key: generated[key] for key in ("scale", "clones", "ledger_bytes", "elapsed_s")}))
    elif args.part in ("check", "measure", "inventory"):
        source = out / f"x{args.scale}"
        generated = json.loads((source / "generated.json").read_text(encoding="utf-8"))
        work = out / "work" / f"x{args.scale}-{args.variant or 'check'}"
        if work.exists():
            shutil.rmtree(work)
        shutil.copytree(source / "case", work)
        try:
            if args.part == "check":
                result = asyncio.run(_check(work, generated, args.sample))
                name = f"check-x{args.scale}.json"
            else:
                started_at = datetime.now(UTC).isoformat()
                result = asyncio.run(_measure(work, scale=args.scale, variant=args.variant, generated=generated,
                                              inventory=args.part == "inventory", steady_s=args.steady_s))
                result.update(started_at=started_at, finished_at=datetime.now(UTC).isoformat())
                result.update(generated={k: generated[k] for k in ("template_sha256", "code_sha256", "ledger_sha256",
                                                                   "counts", "ledger_bytes", "clones")},
                              code_sha256=code_sha256(), host={"platform": platform.platform(),
                                                               "python": platform.python_version(),
                                                               "pythonpath": os.environ.get("PYTHONPATH", "")})
                name = f"{args.part}-x{args.scale}-{args.variant}.json"
        finally:
            shutil.rmtree(work, ignore_errors=True)
        _write(out / name, result)
        print(json.dumps({"part": args.part, "scale": args.scale, "variant": args.variant,
                          "status": result.get("status", "measured")}))
    else:
        prereg = json.loads(args.prereg.read_text(encoding="utf-8"))
        results = [json.loads((out / f"measure-x{scale}-{variant}.json").read_text(encoding="utf-8"))
                   for scale, variant in ORDER if (out / f"measure-x{scale}-{variant}.json").exists()]
        verdict = judge(results, prereg)
        _write(out / "judgement.json", verdict)
        print(json.dumps(verdict))


if __name__ == "__main__":
    main()
