"""The mission-service process: mTLS fleet endpoint, private API socket and the background refresh.

Planner modes: `live` builds the configured provider (MiniMax-M3 by default, D029) from the environment or
`<KEY>_FILE`, and records every live exchange as a `recorded` fixture for later replay; `scripted` answers
from a labelled fixture file and must never be reported as model behaviour; `none` refuses to plan; `auto`
(the resident desk, D035) is `live` when the key is configured and otherwise `scripted` with the M2 suite's
labelled answers. A missing key is reported, never replaced by a mock (D029).

任务服务进程：mTLS 车队端点、私有 API 套接字与后台刷新。

规划模式：`live` 从环境或 `<KEY>_FILE` 构建配置的 provider（默认 MiniMax-M3，D029），并把每次实调交互
录制为 `recorded` 夹具供之后回放；`scripted` 从带标注的夹具文件作答，绝不能当作模型行为报告；`none`
拒绝规划；`auto`（常驻任务台，D035）在配置了 key 时即 `live`，否则为使用 M2 场景集带标注回答的
`scripted`。缺少密钥如实报告，绝不以 mock 代替（D029）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
from pathlib import Path

from drone_agent.fleet.api import serve_api
from drone_agent.fleet.ledger import BusinessLedger
from drone_agent.fleet.service import MissionService
from drone_agent.fleet.transport import FleetHub, serve_fleet
from drone_agent.mission.registry import Registry
from drone_agent.planner.engine import ModelIdentity, PlannerEngine
from drone_agent.planner.replan import ApprovalPolicy
from drone_agent.runtime.ledger import canonical
from drone_agent.runtime.signing import SigningKey


class RecordingPlanner:
    """A fresh engine per request whose live exchanges are saved as `recorded` fixtures.

    每个请求一个新引擎，其实调交互保存为 `recorded` 夹具。
    """

    def __init__(self, provider, identity: ModelIdentity, registry: Registry, tools, recordings: Path, label: str):
        self.provider, self.identity, self.registry, self.tools = provider, identity, registry, tools
        self.recordings, self.label = recordings, label

    async def plan(self, request, *, mission_id: str, mission_version: int = 1):
        from drone_agent.providers import RecordingProvider

        recorder = RecordingProvider(self.provider)
        engine = PlannerEngine(recorder, self.identity, self.registry, self.tools)
        outcome = await engine.plan(request, mission_id=mission_id, mission_version=mission_version)
        recording = recorder.recording(
            source="recorded", provider_id=self.identity.provider_id, model=self.identity.model,
            endpoint_host=self.identity.endpoint_host, prompt_version=engine.prompt_version,
            prompt_sha256=engine.prompt_sha256, software_revision=os.environ.get("DRONE_SOURCE_SHA", "uncommitted"),
            label=f"{mission_id}-v{mission_version}")
        if recording.exchanges:
            recording.save(self.recordings / f"{mission_id}-v{mission_version}.json")
        return outcome


def planner_mode(requested: str) -> str:
    """`auto` resolves to `live` only when the planner key is configured. / 仅在配置了规划 key 时 `auto` 才是 `live`。"""
    if requested != "auto":
        return requested
    from drone_agent.providers.runtime import ProviderUnavailable, provider_config, role_provider_id

    try:
        return "live" if provider_config(role_provider_id("planner")).available else "scripted"
    except ProviderUnavailable:
        return "scripted"


def build_planner(args, registry: Registry):
    """(planner or None, label) for the requested mode. / 按模式返回（规划器或 None，标签）。"""
    mode = planner_mode(args.planner)
    if mode == "none":
        return None, "none"
    from drone_agent.planner.tools.client import StdioSession
    from drone_agent.providers import KeyedScriptedProvider, ProviderUnavailable, build_provider

    if mode == "scripted":
        fixtures = args.fixtures
        if fixtures is None:
            # No key and no fixture file: the suite's labelled answers. / 无 key 且无夹具文件：场景集的带标注回答。
            from drone_agent.eval.m2_prepare import suite_answers

            fixtures = args.state / "planner-fixtures.json"
            fixtures.write_text(json.dumps(suite_answers(args.root), ensure_ascii=False), encoding="utf-8")
        provider = KeyedScriptedProvider.load(fixtures)
        tools = StdioSession(args.root, args.scene).initialize()
        return PlannerEngine(provider, ModelIdentity("scripted", "scripted-fixture"), registry, tools), "scripted"
    try:
        provider, config = build_provider("planner")
    except ProviderUnavailable as error:
        return None, f"unavailable: {error}"
    identity = ModelIdentity(config.provider_id, config.model, config.endpoint_host)
    tools = StdioSession(args.root, args.scene).initialize()
    return RecordingPlanner(provider, identity, registry, tools, args.state / "recordings", "live"), \
        f"live:{config.provider_id}/{config.model}"


async def main_async(args) -> None:
    registry = Registry(args.root, scene=args.scene)
    ledger = BusinessLedger(args.state / "ledger.sqlite3")
    hub = FleetHub(ledger, args.state / "media")
    planner, label = build_planner(args, registry)
    if planner is not None:
        planner.label = label
    service = MissionService(root=args.root, scene=args.scene, ledger=ledger, hub=hub,
                             signing_key=SigningKey.load(args.signing_key),
                             approval_policy=ApprovalPolicy.from_yaml(args.root / "configs/approval_policy.yaml"),
                             planner=planner, robot_id=args.robot_id)
    tls = args.tls
    fleet, port = await serve_fleet(hub, args.listen, cert_pem=(tls / "service.crt").read_bytes(),
                                    key_pem=(tls / "service.key").read_bytes(), ca_pem=(tls / "ca.crt").read_bytes())
    api = await serve_api(service, args.api)
    ready = {"fleet_port": port, "api": str(args.api), "signer_key_id": service.key.key_id, "planner": label,
             "robot_id": args.robot_id, "registry_hash": registry.sha256,
             "source_sha": os.environ.get("DRONE_SOURCE_SHA", "uncommitted")}
    (args.state / "ready.json").write_bytes(canonical(ready))
    print(json.dumps(ready), flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await service.run(stop)
    finally:
        api.close()
        await fleet.stop(2)
        ledger.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/workspace"))
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=Path("/state"))
    parser.add_argument("--signing-key", type=Path, default=Path("/secrets/approval-signing.key"))
    parser.add_argument("--tls", type=Path, default=Path("/tls"))
    parser.add_argument("--listen", default="0.0.0.0:8450")
    parser.add_argument("--api", type=Path, default=Path("/run/mission/api.sock"))
    parser.add_argument("--robot-id", default="uav_01")
    parser.add_argument("--planner", choices=["auto", "live", "scripted", "none"], default="live")
    parser.add_argument("--fixtures", type=Path, help="scripted planner fixture file (scripted mode)")
    args = parser.parse_args()
    if args.planner == "scripted" and not args.fixtures:
        parser.error("--planner scripted requires --fixtures")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
