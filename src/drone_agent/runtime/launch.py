"""Launch one aircraft process with a preapproved local package.

使用本地预批准任务包启动一个机载进程。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import time
from pathlib import Path

from drone_agent.contracts import MissionPackage, RecoveryPolicy
from drone_agent.mission.registry import Registry
from drone_agent.runtime.ipc import GuardianClient, serve
from drone_agent.runtime.ledger import Journal, canonical
from drone_agent.runtime.recording import Recorder


async def main_async(args):
    registry = Registry(args.root)
    package = MissionPackage.model_validate_json(args.package.read_bytes())
    args.artifacts.mkdir(parents=True, exist_ok=True)
    executive_id = "executive:" + package.mission_id
    identity = {
        "pid": os.getpid(),
        "role": args.role,
        "mission_id": package.mission_id,
        "source_sha": os.environ.get("DRONE_SOURCE_SHA", "uncommitted"),
        "registry_hash": registry.sha256,
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
        guardian = Guardian(
            adapter=adapter,
            package=package,
            registry=registry,
            journal=journal,
            policy=RecoveryPolicy.from_yaml(args.root / "configs/recovery_policies/multirotor_m1_v1.yaml"),
            executive_id=executive_id,
        )
        if args.fault:
            from drone_agent.eval.faults import install_injection

            install_injection(guardian, args.fault)
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
                    "monotonic": time.monotonic(),
                }
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
            await server.stop(1)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            (args.artifacts / "adapter-commands.json").write_bytes(canonical(adapter.command_log))
            periods = sorted(guardian.periods)
            (args.artifacts / "supervision.json").write_bytes(
                canonical(
                    {
                        "samples": len(periods),
                        "p99_s": periods[min(int(len(periods) * 0.99), len(periods) - 1)] if periods else None,
                        "max_s": max(periods, default=0),
                    }
                )
            )
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
            )
            await executive.run()
        except Exception as error:
            journal.append("executive_error", {"type": type(error).__name__, "reason": str(error)})
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
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
