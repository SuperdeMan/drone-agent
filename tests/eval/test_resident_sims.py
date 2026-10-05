"""The resident desk simulators' file handling (D069, D072): the world file, flown versions, bounded logs, vendor faults.

常驻任务台模拟器的文件处理（D069、D072）：世界文件、已飞版本、有界日志、厂商故障。
"""

from __future__ import annotations

import json

from drone_agent.eval import s0_fleet, vendor_sim


def world(tmp_path, value) -> object:
    path = tmp_path / "appearance.json"
    path.write_text(json.dumps(value) if not isinstance(value, str) else value, encoding="utf-8")
    return path


def test_the_world_file_gives_each_simulator_only_its_own_known_states(tmp_path):
    path = world(tmp_path, {"format": "drone.desk-world/v1", "s0": {"asset_blue": "damaged", "x": "melted"},
                            "s1": {"asset_red": "damaged"}, "s3": {"road_north": "obstructed"}})
    assert s0_fleet.read_world(path, "s0") == {"asset_blue": "damaged"}
    assert vendor_sim.load_damaged(path) == {"road_north"}
    assert s0_fleet.read_world(world(tmp_path, {"format": "other", "s0": {"a": "damaged"}}), "s0") == {}
    assert s0_fleet.read_world(world(tmp_path, "not json"), "s0") == {}
    assert s0_fleet.read_world(tmp_path / "absent.json", "s0") == {} and s0_fleet.read_world(None, "s0") == {}


def test_a_restarted_fleet_counts_every_version_with_a_flight_directory_as_flown(tmp_path):
    robot = tmp_path / "uav_fa"
    (robot / "aircraft/m-0123456789ab/v1").mkdir(parents=True)
    (robot / "aircraft/m-0123456789ab/v2").mkdir(parents=True)
    (robot / "aircraft/m-0123456789ab/notes").mkdir(parents=True)
    assert s0_fleet.flown_versions(robot) == {"m-0123456789ab-v1.json", "m-0123456789ab-v2.json"}
    assert s0_fleet.flown_versions(tmp_path / "uav_none") == set()


def test_logs_keep_one_previous_file_past_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(s0_fleet, "LOG_LIMIT", 200)
    path = tmp_path / "truth.jsonl"
    for index in range(10):
        s0_fleet.append_rows(path, [{"n": index, "pad": "x" * 40}])
    assert path.with_name("truth.jsonl.1").is_file()
    rows = [json.loads(line) for name in ("truth.jsonl.1", "truth.jsonl") if (tmp_path / name).is_file()
            for line in (tmp_path / name).read_text().splitlines()]
    assert rows[-1]["n"] == 9 and path.stat().st_size <= 400
    s0_fleet.append_rows(path, [])


def test_vendor_faults_come_only_from_the_harness_switch_and_truth_is_appended(tmp_path):
    switch = tmp_path / "vendor.json"
    switch.write_text(json.dumps({"faults": {"duplicate": True, "drop_link_now": "t-1"}}), encoding="utf-8")
    assert vendor_sim.load_faults(switch) == {"duplicate": True, "drop_link_now": "t-1"}
    assert vendor_sim.load_faults(tmp_path / "absent.json") == {} and vendor_sim.load_faults(None) == {}
    sim = vendor_sim.VendorDockSim("dock_vd", "uav_v1", signatures={"asset_red": "red"}, seed=3)
    sim.truth["commands"].append({"at": "2026-10-02T00:00:01+00:00", "method": "flighttask_prepare"})
    sim.truth["executions"].append({"at": "2026-10-02T00:00:02+00:00", "flight_id": "f-1"})
    sim.executed["f-1"] = "ok"
    vendor_sim.flush_truth(sim, tmp_path)
    vendor_sim.flush_truth(sim, tmp_path)
    rows = [json.loads(line) for line in (tmp_path / "truth.jsonl").read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["commands", "executions"]
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["executed"] == {"f-1": "ok"} and state["boot_id"] == sim.boot_id


def test_two_captures_of_one_asset_never_share_bytes_even_across_log_flushes():
    from types import SimpleNamespace

    fleet = s0_fleet.Fleet.__new__(s0_fleet.Fleet)
    fleet.args, fleet.captures, fleet.nonce = SimpleNamespace(world=None), [], 41
    capture = fleet.camera("uav_fa", {"asset_red": "red"})
    first = capture("asset_red")
    fleet.captures.clear()  # what every flush does / 每次刷新都会这样做
    second = capture("asset_red")
    assert first != second and fleet.nonce == 43


async def test_a_resident_dock_backend_keeps_no_transcript():
    """The S0 judge reads the transcript; a resident backend would only grow it (D073, 2026-10-05).

    S0 裁判读取调用记录；常驻后端只会让它无限增长（D073，2026-10-05）。
    """
    from drone_agent.eval.dock_simulator import DockBackend

    async def call(method: str, **params) -> dict:
        return {"ok": True, "result": {}}

    judged = DockBackend("dock:p1-s1-sim", {}, call)
    resident = DockBackend("dock:p1-s1-sim", {}, call, keep_transcript=False)
    for backend in (judged, resident):
        for _ in range(3):
            assert (await backend._call("docks.actions", dock_id="dock_s1"))["ok"]
    assert len(judged.transcript) == 3 and resident.transcript == []


async def test_a_resident_aircraft_syncs_its_uplink_at_the_uplink_rate():
    """The fleet loop runs every 0.25 s, but each uplink pass walks every accepted mission on disk; the resident
    aircraft syncs at the uplink's own 1 Hz (D073, 2026-10-05).

    机队循环每 0.25 s 运行一次，而每次 uplink 处理都遍历磁盘上全部已接受的任务；常驻飞行器按 uplink 自身的 1 Hz 同步
    （D073，2026-10-05）。
    """
    synced: list[int] = []

    class Uplink:
        async def cycle(self) -> None:
            synced.append(1)

    uav = s0_fleet.ResidentUav.__new__(s0_fleet.ResidentUav)
    uav.uplink, uav.task, uav.flights = Uplink(), None, []
    uav.pending = lambda: None
    for _ in range(3):
        await uav.cycle()
    assert len(synced) == 1, "one uplink pass within a second"
    uav.synced -= s0_fleet.SYNC_PERIOD_S
    await uav.cycle()
    assert len(synced) == 2
