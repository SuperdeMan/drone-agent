"""The launcher builds an external-mode guardian only once the egress node is linked, then waits for the local
autonomy before serving (D039, D048).

A late egress node or autonomy layer must lengthen the start, never crash it: the guardian validates an external
package against its live capabilities when it is built.

启动器只在出口节点连通后才构建外部模式 guardian，并在提供服务前等待本地自主层（D039、D048）。

迟到的出口节点或自主层只能让启动变长，绝不能让启动崩溃：guardian 构建时按实时能力校验外部模式任务包。
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import drone_agent.adapters.px4_mavsdk as px4_mavsdk
import drone_agent.autonomy.link as link
from drone_agent.eval.m3_fixture import make_m3_package
from drone_agent.mission.registry import M3_SCENE, Registry
from drone_agent.runtime import launch
from tests.guardian.m3 import Adapter, Endpoint, egress_status, localization, obstacles

ROOT = Path(__file__).resolve().parents[2]


class Served(Exception):
    """Stops the launcher where it would open its control socket. / 在启动器将要打开控制套接字处停下。"""


class LateEndpoint(Endpoint):
    """A role endpoint whose peers start delivering only after a delay. / 对端延迟一段时间才开始交付的角色端点。"""

    def __init__(self, role, *_args, **_kwargs):
        super().__init__()
        self.role = role
        self.counters = SimpleNamespace()

    async def start(self):
        delay, feed = FEEDS[self.role]

        async def deliver():
            await asyncio.sleep(delay)
            while True:
                feed(self)
                await asyncio.sleep(0.1)

        self.task = asyncio.create_task(deliver())

    async def close(self):
        self.task.cancel()


def feed_egress(endpoint):
    endpoint.put("egress_status", egress_status())


def feed_autonomy(endpoint):
    endpoint.put("localization_report", localization())
    endpoint.put("obstacle_set", obstacles())


FEEDS = {link.Role.EGRESS: (0.4, feed_egress), link.Role.AUTONOMY: (0.8, feed_autonomy)}


class LaunchAdapter(Adapter):
    def __init__(self, registry, *_args):
        super().__init__(registry)

    async def connect(self):
        return None


async def test_a_late_egress_node_and_autonomy_layer_delay_the_start_but_never_crash_it(tmp_path, monkeypatch):
    # SITL: building the guardian before the egress node reported refused the package as unsupported and the case
    # never flew. / SITL：出口节点上报之前就构建 guardian，任务包被判为不支持，该用例根本没有起飞。
    registry = Registry(ROOT, scene=ROOT / M3_SCENE)
    package = make_m3_package(registry, 7, "m3-launch", "inspect_external")
    (tmp_path / "package.json").write_text(package.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(px4_mavsdk, "Px4Adapter", LaunchAdapter)
    monkeypatch.setattr(link, "LocalEndpoint", LateEndpoint)
    built = []

    async def serve(guardian, endpoint):
        built.append(guardian)
        raise Served

    monkeypatch.setattr(launch, "serve", serve)
    args = SimpleNamespace(
        role="guardian", root=ROOT, scene=ROOT / M3_SCENE, package=tmp_path / "package.json", trust=None,
        artifacts=tmp_path / "aircraft", sensor=tmp_path / "sensor.json", robot_state=None,
        egress_socket=tmp_path / "egress.sock", autonomy_socket=tmp_path / "autonomy.sock", egress_wait_s=5.0,
        simulation=True, fault=None, endpoint="unix:" + str(tmp_path / "ipc" / "guardian.sock"),
    )
    with pytest.raises(Served):
        await launch.main_async(args)
    assert built and built[0].external is not None
    rows = [json.loads(line) for line in (tmp_path / "aircraft/guardian.jsonl").read_text().splitlines()]
    ready = next(row["data"] for row in rows if row["kind"] == "external_ready")
    assert ready["ready"] and ready["egress"] and ready["autonomy"]
    for endpoint in (built[0].external.egress, built[0].external.autonomy):
        await endpoint.close()
