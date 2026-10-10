"""The service's planner across sites: each request is planned against the site it will fly at (D079).

A bound request (catalog mode, D055) flies at its robot's site, so the model must see that site's map: its volumes and
assets in the draft schema, and its read-only tool results in SITE_DATA. One tool session per site map is opened on
first use and kept; the engine itself is built per request, so a live exchange can be recorded per request as before.
An unbound request (the M2 deployment) uses the planner's default site. Nothing here compiles, admits or approves.

服务跨站点的规划器：每个请求按它将要飞行的站点规划（D079）。

绑定请求（目录模式，D055）在其机器人的站点飞行，所以模型必须看到该站点的地图：草案 schema 中的体积与资产，以及
SITE_DATA 中的只读工具结果。每个站点地图的工具会话在第一次用到时打开并保留；引擎按请求构建，因此实调交互仍按请求录制。
未绑定的请求（M2 部署）使用规划器的默认站点。这里不编译、不准入、也不审批。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from drone_agent.admission.models import MissionRequest
from drone_agent.mission.registry import Registry
from drone_agent.planner.engine import ModelIdentity, PlannerEngine, PlannerOutcome
from drone_agent.providers.replay import RecordingProvider


def site_key(scene: Path) -> str:
    return str(Path(scene).resolve())


class SitePlanner:
    """Plans each request with the map and read-only tools of one site; holds no write capability.

    `provider` and `identity` are what provenance and the workflow drafter read (D029, D057 §9). `session` opens the
    read-only tool session of a site map; without it only the default site can be planned. With `recordings`, every
    live exchange is saved as a `recorded` fixture named after the mission version.

    用某个站点的地图与只读工具规划每个请求；不持有任何写能力。`provider` 与 `identity` 供来源记录与工作流草案读取
    （D029，D057 §9）。`session` 打开站点地图的只读工具会话；没有它就只能规划默认站点。给出 `recordings` 时，每次
    实调交互都按任务版本保存为 `recorded` 夹具。
    """

    def __init__(self, provider, identity: ModelIdentity, registry: Registry, tools, *, scene: Path | None = None,
                 session: Callable[[Path], object] | None = None, recordings: Path | None = None,
                 **engine_options):
        self.provider, self.identity = provider, identity
        self.default = (registry, tools)
        self.session, self.recordings, self.engine_options = session, recordings, engine_options
        self.sessions: dict[str, object] = {} if scene is None else {site_key(scene): tools}
        self.label = ""

    def tools_for(self, scene: Path):
        """The read-only tool session of one site map, opened on first use. / 某站点地图的只读工具会话，首次使用时打开。"""
        key = site_key(scene)
        if key not in self.sessions:
            if self.session is None:
                raise ValueError(f"no tool session for {Path(scene).name}: this planner serves one site only")
            self.sessions[key] = self.session(Path(scene))
        return self.sessions[key]

    async def plan(self, request: MissionRequest, *, mission_id: str, mission_version: int = 1,
                   site: tuple[Registry, Path] | None = None) -> PlannerOutcome:
        """Plan once at `site` (the robot's registry and its scene file), or at the default site.

        在 `site`（机器人的登记表及其场景文件）或默认站点规划一次。
        """
        registry, tools = self.default if site is None else (site[0], self.tools_for(site[1]))
        provider = RecordingProvider(self.provider) if self.recordings is not None else self.provider
        engine = PlannerEngine(provider, self.identity, registry, tools, **self.engine_options)
        outcome = await engine.plan(request, mission_id=mission_id, mission_version=mission_version)
        if self.recordings is not None:
            recording = provider.recording(
                source="recorded", provider_id=self.identity.provider_id, model=self.identity.model,
                endpoint_host=self.identity.endpoint_host, prompt_version=engine.prompt_version,
                prompt_sha256=engine.prompt_sha256,
                software_revision=os.environ.get("DRONE_SOURCE_SHA", "uncommitted"),
                label=f"{mission_id}-v{mission_version}")
            if recording.exchanges:
                recording.save(self.recordings / f"{mission_id}-v{mission_version}.json")
        return outcome

    def close(self) -> None:
        """Close every tool session this planner opened. / 关闭本规划器打开的全部工具会话。"""
        for tools in self.sessions.values():
            close = getattr(tools, "close", None)
            if close is not None:
                close()
