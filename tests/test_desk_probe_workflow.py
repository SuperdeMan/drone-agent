"""The desk workflow probe follows reinspection runs even when the page pushes run frames between its replies.

The page pushes the watched run whenever it changes, so a reply can arrive behind a pushed frame. When a run
completed and started its reinspection run while the probe slept, the probe once read the pushed frame as the reply,
never tracked the new run and stopped with only the first mission: a receipt that could only fail the gate.

任务台工作流探针即使在应答之间收到页面推送的运行帧，也要跟踪复检运行。页面在被监视运行变化时会推送该运行，
应答因此可能排在推送帧之后。运行在探针等待期间完成并启动复检运行时，探针曾把推送帧当作应答，从未跟踪新运行，
只带着第一个任务就结束：这样的回执只会让门禁失败。
"""

from __future__ import annotations

import argparse
import runpy
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = runpy.run_path(str(ROOT / "scripts/desk_probe.py"))


def run_view(run_id: str, workflow_id: str, state: str, missions: list[str], children: tuple[str, ...] = ()) -> dict:
    return {"run": {"run_id": run_id, "workflow_id": workflow_id, "version": 1, "state": state,
                    "trigger_source": "manual:tailnet:<operator>", "started_by": "tailnet:<operator>", "outcome": None},
            "waiting": ["approval"] if state == "waiting" else [], "nodes": [], "analyses": [], "reviews": [],
            "orders": [], "missions": [{"mission_id": m} for m in missions],
            "children": [{"run_id": c} for c in children]}


def mission_view(mission_id: str, status: str) -> dict:
    judged = status == "completed"
    return {"mission": {"mission_id": mission_id, "status": status},
            "versions": [{"version": 1, "status": status, "package_hash": "h"}],
            "binding": {"project_id": "campus_s1"}, "dispatch": {},
            "request": {"channel": "workflow", "requested_by": "workflow:wr-root"}, "report": None,
            "cloud": {"flights": [{"version": 1}] if judged else [], "judge": {"passed": True} if judged else None}}


class FakeDesk:
    """One page session: replies to each frame and pushes the watched run when it changed. / 一个页面会话。"""

    def __init__(self) -> None:
        self.queue = deque([{"type": "hello", "identity": "tailnet:someone", "can_write": True, "planner": "x"}])
        self.stage, self.watched, self.approved = 0, None, []

    def runs(self) -> dict:
        root = "completed" if self.stage else "waiting"
        views = {"wr-root": run_view("wr-root", "asset_check", root, ["m-1"], ("wr-child",) if self.stage else ())}
        if self.stage:
            views["wr-child"] = run_view("wr-child", "asset_reinspection", "completed" if self.stage == 2 else "waiting",
                                         ["m-2"])
        return views

    def missions(self) -> dict:
        first = "completed" if self.stage else "awaiting_approval"
        second = {1: "awaiting_approval", 2: "completed"}.get(self.stage)
        return {"m-1": mission_view("m-1", first), **({"m-2": mission_view("m-2", second)} if second else {})}

    def send(self, frame: dict) -> None:
        kind = frame["type"]
        if kind == "workflows":
            self.queue.append({"type": "workflows", "view": {"roles": ["operator"], "catalog": {}, "templates": []}})
        elif kind in ("workflow_start", "workflow_watch"):
            self.watched = frame.get("run_id") or "wr-root"
            self.queue.append({"type": "workflow", "view": self.runs()[self.watched]})
        elif kind == "watch":
            self.queue.append({"type": "mission", "view": self.missions()[frame["mission_id"]]})
        elif kind == "approve":
            self.approved.append(frame["mission_id"])
        else:
            self.queue.append({"type": "error", "message": f"unknown type {kind!r}"})

    def advance(self) -> None:
        """While the probe sleeps: the world moves on and the page pushes the changed watched run. / 探针等待期间。"""
        before = self.runs().get(self.watched)
        self.stage = min(self.stage + 1, 2)
        after = self.runs().get(self.watched)
        if after != before:
            self.queue.append({"type": "workflow", "view": after})

    def next(self, kinds, timeout: float) -> dict:
        kinds = (kinds,) if isinstance(kinds, str) else tuple(kinds)
        while self.queue:
            message = self.queue.popleft()
            if message["type"] in kinds:
                return message
        raise TimeoutError(f"no {kinds}")

    def close(self) -> None:
        pass


def test_a_reinspection_run_started_while_the_probe_slept_is_followed_to_its_end(monkeypatch):
    desk = FakeDesk()
    workflow = PROBE["workflow"]
    monkeypatch.setitem(workflow.__globals__, "Client", lambda origin: desk)
    monkeypatch.setattr(workflow.__globals__["time"], "sleep", lambda seconds: desk.advance())
    args = argparse.Namespace(project="campus_s1", draft_text=None, start=True, workflow="asset_check",
                              asset_input="asset_red", review="confirmed", plan_timeout=30, timeout=60)
    receipt, _ = workflow("https://desk.example", args)
    assert set(receipt["runs"]) == {"wr-root", "wr-child"}, receipt["timeline"]
    assert {r["run"]["state"] for r in receipt["runs"].values()} == {"completed"}
    assert set(receipt["missions"]) == {"m-1", "m-2"} and desk.approved == ["m-1", "m-2"]
