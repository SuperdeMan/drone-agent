"""Launch one aircraft process with a preapproved package.

M1 reads a locally trusted package. M2 (D030) adds `--trust`: the package must carry an approval signed by
a key in the read-only trust file, verified independently by each process, and `--robot-state` keeps the
robot's epoch watermark and accepted versions across mission versions. `--scene` selects the registry and `--operator-mailbox` reads operator requests from the uplink's mailbox.

使用预批准任务包启动一个机载进程。

M1 读取本地可信任务包。M2（D030）增加 `--trust`：任务包必须带有由只读信任文件中的密钥签署的审批，
由每个进程独立验签；`--robot-state` 跨任务版本保存机器人的代次水位与已接受版本；`--scene` 选择登记表，`--operator-mailbox` 从 uplink 的信箱读取操作请求。

M3 (D039, D042): the recovery policy is loaded from the package's own reference, never from a fixed file. Under
policy v2 the guardian serves the role-bound egress and autonomy sockets and, when the package contains
external-mode skills, waits a bounded time for the egress node before it validates the package against its live
capability; the executive serves the belief socket. Autonomy traffic is recorded to MCAP, not to the fsync'd journal.

M3（D039、D042）：恢复策略按任务包自己的引用加载，不再读固定文件。策略 v2 下 guardian 提供按角色划分的出口与自主层
套接字；任务包含外部模式技能时，先有界等待出口节点，再按实时能力校验任务包；executive 提供信念套接字。自主层流量
写入 MCAP，而不是逐行 fsync 的账本。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import time
from pathlib import Path

from drone_agent.contracts import MissionPackage, RecoveryPolicy
from drone_agent.guardian.external import EXTERNAL_SKILLS
from drone_agent.mission.registry import Registry
from drone_agent.runtime.ipc import GuardianClient, serve
from drone_agent.runtime.ledger import Journal, canonical
from drone_agent.runtime.recording import Recorder
from drone_agent.runtime.signing import TrustStore


async def stop_guardian_tasks(server, tasks):
    """Quiesce observations before RPC shutdown can outlive telemetry. / 先停止观测发布，避免 RPC 停机等待超出遥测寿命。"""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await server.stop(1)


def load_policy(root: Path, reference: str) -> RecoveryPolicy:
    """The recovery policy a package names, e.g. multirotor_m3@v2 -> multirotor_m3_v2.yaml.

    任务包指定的恢复策略，例如 multirotor_m3@v2 -> multirotor_m3_v2.yaml。
    """
    policy_id, _, version = reference.partition("@")
    if not policy_id.replace("_", "").isalnum() or not version.isalnum():
        raise ValueError("malformed recovery policy reference")
    policy = RecoveryPolicy.from_yaml(root / "configs/recovery_policies" / f"{policy_id}_{version}.yaml")
    if (policy.policy_id, policy.version) != (policy_id, version):
        raise ValueError("recovery policy file does not match its reference")
    return policy


async def wait_for_external(guardian, seconds: float) -> dict:
    """Wait until the external mode's dependencies deliver before serving an external-mode mission.

    That is the egress node registered, compatible and linked to PX4 (D039), and the local autonomy fresh with an
    informative localization report (D048). The outcome is journaled; on timeout the guardian still starts, and the
    checks at the start of each external step stay the safety net.

    在为外部模式任务提供服务之前，等待外部模式的依赖开始交付：出口节点已注册、兼容且与 PX4 连通（D039），本地自主层
    新鲜且定位报告有结论（D048）。结果写入账本；超时后 guardian 仍会启动，每个外部步骤开始时的检查依旧兜底。
    """
    start = time.monotonic()
    while True:
        egress, autonomy = guardian.external.available(), guardian.external.autonomy_fresh()
        if (egress and autonomy) or time.monotonic() - start >= seconds:
            return {"ready": egress and autonomy, "egress": egress, "autonomy": autonomy,
                    "waited_s": round(time.monotonic() - start, 2)}
        await asyncio.sleep(0.2)


async def main_async(args):
    registry = Registry(args.root, scene=args.scene)
    package = MissionPackage.model_validate_json(args.package.read_bytes())
    trust = TrustStore.load(args.trust) if args.trust else None
    args.artifacts.mkdir(parents=True, exist_ok=True)
    executive_id = "executive:" + package.mission_id
    identity = {
        "pid": os.getpid(),
        "role": args.role,
        "mission_id": package.mission_id,
        "source_sha": os.environ.get("DRONE_SOURCE_SHA", "uncommitted"),
        "registry_hash": registry.sha256,
        "trust_mode": "signed" if trust else "local_file",
        "trust_signers": trust.key_ids if trust else [],
    }
    (args.artifacts / f"{args.role}-identity.json").write_bytes(canonical(identity))
    if args.role == "guardian":
        from drone_agent.adapters.px4_mavsdk import Px4Adapter
        from drone_agent.guardian.core import Guardian

        adapter = Px4Adapter(registry, args.artifacts, args.sensor)
        await adapter.connect()
        (args.artifacts / "capabilities.json").write_text(adapter.capabilities.model_dump_json(indent=2))
        journal = Journal(args.artifacts / "guardian.jsonl")
        recorder = Recorder(args.artifacts / "guardian.mcap")
        robot_state = None
        if args.robot_state:
            from drone_agent.runtime.robot_state import RobotAuthorityState

            robot_state = RobotAuthorityState(args.robot_state, registry.capability.robot_id)
        policy = load_policy(args.root, package.recovery_policy_ref)
        endpoints = []
        egress = autonomy = None
        if policy.version == "v2":
            from drone_agent.autonomy.link import LocalEndpoint, Role

            def recorded(kind, model):
                recorder.write("autonomy/" + kind, model.model_dump(mode="json"))

            egress = LocalEndpoint(Role.EGRESS, args.egress_socket, on_message=recorded)
            autonomy = LocalEndpoint(Role.AUTONOMY, args.autonomy_socket, on_message=recorded)
            endpoints = [egress, autonomy]
            for endpoint in endpoints:
                await endpoint.start()
        guardian = Guardian(
            adapter=adapter,
            package=package,
            registry=registry,
            journal=journal,
            policy=policy,
            executive_id=executive_id,
            simulation=args.simulation,
            trust=trust,
            robot_state=robot_state,
            egress=egress,
            autonomy=autonomy,
            log=recorder.write,
        )
        if args.fault:
            from drone_agent.eval.faults import install_injection

            install_injection(guardian, args.fault)
        if guardian.external is not None and any(node.skill_id in EXTERNAL_SKILLS for node in package.nodes):
            ready = await wait_for_external(guardian, args.egress_wait_s)
            journal.append("external_ready", {"mission_id": package.mission_id, **ready,
                                              "links": {e.role.value: e.counters.__dict__ for e in endpoints}})
        socket_dir = Path(args.endpoint.removeprefix("unix:")).parent
        socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(socket_dir, 0o700)
        server = await serve(guardian, args.endpoint)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        async def observe():
            while True:
                value = adapter.snapshot().model_dump(mode="json")
                recorder.write("flight/observation", value)
                status = {
                    "observation": value,
                    "safety": guardian.safety.value,
                    "reason": guardian.reason,
                    "phase": guardian.phase,
                    "active_step": guardian.active_step.task_id if guardian.active_step else None,
                    "command_pending": bool(guardian.dispatch_task and not guardian.dispatch_task.done()),
                    "monotonic": time.monotonic(),
                }
                if guardian.external is not None:
                    status["external"] = {
                        "active": guardian.external.active,
                        "entering": guardian.external.entering,
                        "nav_state": guardian.external.nav_state,
                        "available": guardian.external.available(),
                        "counters": dict(guardian.external.counters),
                    }
                    status["energy"] = {k: v for k, v in (guardian.energy_context or {}).items()
                                        if isinstance(v, (bool, int, float, str)) or v is None}
                temporary = args.artifacts / "status.pending.json"
                temporary.write_bytes(canonical(status))
                temporary.replace(args.artifacts / "status.json")
                await asyncio.sleep(0.1)

        tasks = [asyncio.create_task(guardian.supervise()), asyncio.create_task(observe())]
        try:
            done, _ = await asyncio.wait(
                [*tasks, asyncio.create_task(stop.wait())], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
        finally:
            await stop_guardian_tasks(server, tasks)
            (args.artifacts / "adapter-commands.json").write_bytes(canonical(adapter.command_log))
            periods = sorted(guardian.periods)
            summary = {
                "samples": len(periods),
                "p99_s": periods[min(int(len(periods) * 0.99), len(periods) - 1)] if periods else None,
                "max_s": max(periods, default=0),
            }
            if guardian.external is not None:
                summary["external"] = {"counters": dict(guardian.external.counters),
                                       "intent_latency_p99_ms": guardian.external.latency_p99(),
                                       "intent_latency_samples": len(guardian.external.latency_ms)}
                summary["links"] = {endpoint.role.value: endpoint.counters.__dict__ for endpoint in endpoints}
            (args.artifacts / "supervision.json").write_bytes(canonical(summary))
            for endpoint in endpoints:
                await endpoint.close()
            recorder.close()
            journal.close()
            await adapter.close()
    else:
        from drone_agent.mission.executive import Executive

        client = GuardianClient(args.endpoint)
        journal, recorder = Journal(args.artifacts / "executive.jsonl"), Recorder(args.artifacts / "executive.mcap")
        try:
            await asyncio.wait_for(client.channel.channel_ready(), 45)
            executive = Executive(
                client=client,
                registry=registry,
                package=package,
                journal=journal,
                recorder=recorder,
                artifacts=args.artifacts,
                executive_id=executive_id,
                epoch=args.epoch,
                trust=trust,
                mailbox=args.operator_mailbox,
            )
            belief = None
            if args.belief_socket:
                from drone_agent.autonomy.link import LocalEndpoint, Role

                belief = LocalEndpoint(
                    Role.BELIEF,
                    args.belief_socket,
                    on_message=lambda kind, message: executive.accept_fact(message),
                    on_reject=lambda kind, reason: executive.reject_fact(kind, reason),
                )
                await belief.start()
            try:
                await executive.run()
            finally:
                if belief is not None:
                    await belief.close()
                    (args.artifacts / "belief.json").write_bytes(
                        canonical({"counts": executive.fact_counts, "link": belief.counters.__dict__})
                    )
        except Exception as error:
            row = journal.append("executive_error", {"type": type(error).__name__, "reason": str(error)})
            recorder.write("mission/events", row)
            raise
        finally:
            journal.close()
            recorder.close()
            await client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=["guardian", "executive"])
    parser.add_argument("--root", type=Path, default=Path("/workspace"))
    parser.add_argument("--package", type=Path, default=Path("/input/package.json"))
    parser.add_argument("--artifacts", type=Path, default=Path("/artifacts"))
    parser.add_argument("--sensor", type=Path, default=Path("/sensor/latest.json"))
    parser.add_argument("--endpoint", default="unix:/run/drone/guardian.sock")
    parser.add_argument("--epoch", type=int, default=1)
    parser.add_argument("--fault", type=Path)
    parser.add_argument("--simulation", action="store_true")
    parser.add_argument("--scene", type=Path, help="registry file; defaults to the M1 campus scene")
    parser.add_argument("--trust", type=Path, help="read-only trust file; requires signed approvals (M2)")
    parser.add_argument("--robot-state", type=Path, help="robot-level authority state file (guardian, M2)")
    parser.add_argument("--operator-mailbox", type=Path, help="operator mailbox written by the uplink (executive, M2)")
    parser.add_argument("--egress-socket", type=Path, default=Path("/run/egress/egress.sock"),
                        help="egress-node socket served by the guardian under policy v2 (M3)")
    parser.add_argument("--autonomy-socket", type=Path, default=Path("/run/autonomy/autonomy.sock"),
                        help="autonomy-node socket served by the guardian under policy v2 (M3)")
    parser.add_argument("--egress-wait-s", type=float, default=60.0,
                        help="bounded wait for the egress node and the local autonomy before serving an "
                             "external-mode package (M3, D048)")
    parser.add_argument("--belief-socket", type=Path, help="belief-fact socket served by the executive (M3)")
    args = parser.parse_args()
    if args.fault and not args.simulation:
        parser.error("fault injection requires an explicit simulation process")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
