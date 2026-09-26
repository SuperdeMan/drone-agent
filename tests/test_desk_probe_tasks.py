"""The desk task probe: it records the queue, is refused every control frame, approves only the active assignment's
mission and follows the task until it is final and its flight was judged, even when pushed frames interleave.

任务台任务单探针：记录队列，每个控制帧都被拒绝，只审批有效分配的任务，并在推送帧穿插时仍跟随任务单直到终结且其飞行有裁判
结果。
"""

from __future__ import annotations

import argparse
import runpy
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = runpy.run_path(str(ROOT / "scripts/desk_probe.py"))


class FakeDesk:
    """One page session over a task that is withdrawn from uav_01 and then flown by uav_02. / 一个页面会话。"""

    def __init__(self) -> None:
        self.queue = deque([{"type": "hello", "identity": "tailnet:someone", "can_write": True, "planner": "x",
                             "projects": [{"project_id": "campus_s1", "legacy": False, "roles": ["operator"],
                                           "robots": ["uav_01"], "scheduling": True}]}])
        self.stage, self.approved = 0, []

    def view(self) -> dict:
        assignments = [{"epoch": 1, "robot_id": "uav_01", "mission_id": "m-1",
                        "state": "withdrawn" if self.stage else "active", "reason": None}]
        if self.stage:
            assignments.append({"epoch": 2, "robot_id": "uav_02", "mission_id": "m-2", "state": "active",
                                "reason": None})
        state = {0: "assigned", 1: "assigned", 2: "completed"}[self.stage]
        return {"task": {"task_id": "tk-1", "project_id": "campus_s1", "asset_id": "asset_red",
                         "volume_id": "campus_training", "state": state, "epoch": 2 if self.stage else 1,
                         "robot_id": "uav_02" if self.stage else "uav_01", "mission_id": None, "reason": None,
                         "source": "operator", "requested_by": "tailnet:someone", "outcome": None, "waiting": None},
                "assignments": assignments, "decisions": [], "events": []}

    def mission(self, mission_id: str) -> dict:
        status = {"m-1": "withdrawn" if self.stage else "awaiting_approval",
                  "m-2": "completed" if self.stage == 2 else "awaiting_approval"}[mission_id]
        judged = status == "completed"
        return {"mission": {"mission_id": mission_id, "status": status},
                "versions": [{"version": 1, "status": status, "package_hash": "h"}],
                "binding": {"project_id": "campus_s1"}, "dispatch": {},
                "request": {"channel": "scheduler", "requested_by": "task:tk-1"}, "report": None,
                "cloud": {"flights": [{"version": 1}] if judged else [], "judge": {"passed": True} if judged else None}}

    def send(self, frame: dict) -> None:
        kind = frame["type"]
        if kind == "tasks_watch":
            self.queue.append({"type": "tasks", "view": {"roles": ["operator"], "catalog": {}, "assets": {},
                                                         "robots": [], "tasks": [], "airspace": {"holds": []}}})
        elif kind in ("task_submit", "task_watch"):
            self.queue.append({"type": "task", "view": self.view()})
        elif kind == "watch":
            self.queue.append({"type": "mission", "view": self.mission(frame["mission_id"])})
        elif kind == "approve":
            self.approved.append(frame["mission_id"])
        else:
            self.queue.append({"type": "error", "message": f"unknown type {kind!r}"})

    def advance(self) -> None:
        self.stage = min(self.stage + 1, 2)
        # The page pushes the watched task when it changes. / 被监视的任务单变化时页面推送。
        self.queue.append({"type": "task", "view": self.view()})

    def next(self, kinds, timeout: float) -> dict:
        kinds = (kinds,) if isinstance(kinds, str) else tuple(kinds)
        while self.queue:
            message = self.queue.popleft()
            if message["type"] in kinds:
                return message
        raise TimeoutError(f"no {kinds}")

    def close(self) -> None:
        pass


def test_the_probe_is_refused_control_frames_and_approves_only_active_assignments(monkeypatch):
    desk = FakeDesk()
    tasks = PROBE["tasks"]
    monkeypatch.setitem(tasks.__globals__, "Client", lambda origin: desk)
    monkeypatch.setattr(tasks.__globals__["time"], "sleep", lambda seconds: desk.advance())
    args = argparse.Namespace(project="campus_s1", start=True, asset="asset_red", volume="campus_training", timeout=60)
    receipt, _ = tasks("https://desk.example", args)
    assert len(receipt["refused"]) == len(PROBE["TASK_FORBIDDEN"])
    assert all("unknown type" in r["error"] for r in receipt["refused"])
    assert receipt["projects"][0]["scheduling"] is True
    assert desk.approved == ["m-1", "m-2"], "each active assignment's mission once; never the withdrawn one again"
    assert receipt["task"]["task"]["state"] == "completed" and set(receipt["missions"]) == {"m-1", "m-2"}
    assert [(s["state"], s["robot_id"], s["epoch"]) for s in receipt["timeline"]] == [
        ("assigned", "uav_01", 1), ("assigned", "uav_02", 2), ("completed", "uav_02", 2)]
