"""Shutdown cannot overwrite terminal telemetry during RPC draining. / RPC 停机等待不得覆写终态遥测。"""

import asyncio

from drone_agent.runtime.launch import stop_guardian_tasks


async def test_shutdown_does_not_publish_unknown_after_the_terminal_observation():
    disconnected = asyncio.Event()
    status = {"armed": False, "in_air": False}

    async def observe():
        await disconnected.wait()
        status.update(armed=None, in_air=None)

    class Server:
        async def stop(self, _grace):
            disconnected.set()
            await asyncio.sleep(0)

    observer = asyncio.create_task(observe())
    await asyncio.sleep(0)
    await stop_guardian_tasks(Server(), [observer])
    assert status == {"armed": False, "in_air": False}
    assert observer.cancelled()
