"""Vendor-managed dock tasks (P5, D072): the protocol profile, the task compiled from an approved package, the envelope.

A vendor-managed dock runs its own flight controller, lid, charging and local safety; the platform may only hand it an
authorized high-level task and read what the vendor chooses to report (D053). The profile pins one protocol version:
four whitelisted commands, what an acknowledgement means (receipt only), the events, the deduplication window, the
reconnect behaviour, what is visible and who is responsible for safety. A profile that declares any low-level control
or another command does not load. The task is derived deterministically from the approved mission package, so the
gateway can recompute it at the claim and refuse a task that is not what the human approved. Nothing here claims to
match any real vendor firmware: the structure follows publicly documented dock-cloud task flows, the field names are
this project's own.

厂商托管机场任务（P5，D072）：协议档案、由已批准任务包编译的任务与协议信封。

厂商托管机场自己运行飞控、舱盖、补能与本地安全；平台只能交给它一个已授权的高层任务，并读取厂商选择上报的内容（D053）。
档案固定一个协议版本：四个白名单命令、ACK 的含义（只是受理）、事件、去重窗口、重连行为、可见性与安全责任。声明任何低层
控制或其他命令的档案不能加载。任务由已批准任务包确定性推导，网关因此能在领取时重新推导，拒绝不是人所批准的任务。这里不
宣称与任何真实厂商固件一致：结构参照公开的机场云任务流程，字段名是本项目自定。
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ConfigDict, Field, model_validator

from drone_agent.contracts import MissionPackage
from drone_agent.contracts.common import ContractModel
from drone_agent.fleet.provenance import digest

PROFILE_FORMAT = "drone.vendor-profile/v1"
TASK_FORMAT = "drone.vendor-task/v1"
COMPILER = "vendor-task-v1"
# The only commands a gateway may ever send, by role (D072). / 网关能发送的全部命令，按角色（D072）。
COMMANDS: dict[str, str] = {"prepare": "flighttask_prepare", "execute": "flighttask_execute",
                            "cancel_before_takeoff": "flighttask_undo", "cancel_in_flight": "return_home"}
WHITELIST = frozenset(COMMANDS.values())
TERMINAL = frozenset({"ok", "partially_done", "failed", "canceled", "rejected", "timeout"})
STATUSES = ("sent", "in_progress", "paused", *sorted(TERMINAL))
NAMESPACE_ID = uuid.UUID("6f1b3c2e-5d4a-4b8e-9c7f-2a1e0d3b4c5f")


class VendorError(ValueError):
    """A profile, task or message outside the pinned protocol. / 超出固定协议的档案、任务或报文。"""


class VendorModel(ContractModel):
    """Frozen, strict records of the vendor protocol. / 厂商协议的冻结、严格记录。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["0.1.0"] = "0.1.0"


# ── profile / 档案 ──


class ProtocolInfo(VendorModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,40}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+$")
    modeled_on: str = Field(min_length=1, max_length=300)
    verified_against_firmware: Literal[False]


class CommandSpec(VendorModel):
    """One whitelisted command: its method, resend period and how long to wait for any answer.

    一个白名单命令：方法名、重发周期与等待任何答复的时限。
    """

    method: str
    resend_s: float = Field(default=2.0, gt=0, le=60)
    give_up_s: float = Field(default=60.0, gt=0, le=3600)


class EventSpec(VendorModel):
    progress: Literal["flighttask_progress"]
    media: Literal["file_upload_callback"]
    return_home: Literal["return_home_info"]
    state: Literal["dock_state"]
    statuses: tuple[str, ...]

    @model_validator(mode="after")
    def _statuses(self):
        if set(self.statuses) != set(STATUSES):
            raise ValueError(f"progress statuses must be exactly {', '.join(STATUSES)}")
        return self


class Reconnect(VendorModel):
    replays_last_progress: bool


class Visibility(VendorModel):
    """What the platform can see; logs and safety events of a vendor dock are not visible.

    平台能看到的内容；厂商机场的日志与安全事件不可见。
    """

    telemetry: tuple[Literal["position", "battery", "flight_mode", "dock_lid", "aircraft_in_dock", "charging",
                             "wind"], ...]
    logs: Literal["none"]
    safety_events: Literal["none"]


class VendorProfile(VendorModel):
    """One pinned protocol version of a vendor-managed dock (D072). / 厂商托管机场的一个固定协议版本（D072）。"""

    format: Literal["drone.vendor-profile/v1"]
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    protocol: ProtocolInfo
    task_types: tuple[Literal["waypoint_inspection"], ...] = Field(min_length=1)
    commands: dict[str, CommandSpec]
    ack_semantics: Literal["accepted_only"]
    events: EventSpec
    dedup_window_s: float = Field(gt=0, le=86400)
    reconnect: Reconnect
    media_grace_s: float = Field(gt=0, le=600)
    state_period_s: float = Field(gt=0, le=10)
    visibility: Visibility
    safety_responsibility: Literal["vendor"]
    payloads: tuple[Literal["rgb_camera"], ...] = Field(min_length=1)
    cross_dock: Literal[False]
    low_level_control: Literal["none"]

    @model_validator(mode="after")
    def _commands(self):
        if set(self.commands) != set(COMMANDS):
            raise ValueError("the profile names exactly the four whitelisted command roles")
        for role, spec in self.commands.items():
            if spec.method != COMMANDS[role]:
                raise ValueError(f"{role} must be {COMMANDS[role]}, not {spec.method}")
        return self

    def method(self, role: str) -> str:
        return self.commands[role].method


def load_profile(root: Path, path: str) -> tuple[VendorProfile, str]:
    """(profile, file SHA-256); the file must lie inside the repository. / （档案，文件 SHA-256）；文件须在仓库内。"""
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or not (root / relative).is_file():
        raise VendorError(f"vendor profile {path} is missing or outside the repository")
    data = (root / relative).read_bytes()
    try:
        profile = VendorProfile.model_validate(yaml.safe_load(data.decode("utf-8")))
    except ValueError as error:
        raise VendorError(f"vendor profile {path} rejected: {error}") from error
    return profile, hashlib.sha256(data).hexdigest()


# ── task / 任务 ──


class TaskFrame(VendorModel):
    frame_id: str
    map_version: str


class VendorStep(VendorModel):
    """One step of a vendor waypoint task, mapped to the package node it serves. / 厂商航点任务的一步，对应其服务的任务包节点。"""

    index: int = Field(ge=0)
    kind: Literal["takeoff", "waypoint", "photo", "land"]
    node: str = Field(min_length=1, max_length=80)
    position: tuple[float, float, float]
    asset_id: str | None = None


class VendorTask(VendorModel):
    """The high-level task a vendor dock receives, derived from one approved package. / 厂商机场收到的高层任务。"""

    format: Literal["drone.vendor-task/v1"] = TASK_FORMAT
    compiler: Literal["vendor-task-v1"] = COMPILER
    profile_id: str
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    flight_id: str
    mission_id: str
    mission_version: int = Field(ge=1)
    package_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    robot_id: str
    dock_id: str
    task_type: Literal["waypoint_inspection"] = "waypoint_inspection"
    frame: TaskFrame
    speed_mps: float = Field(gt=0, le=15)
    steps: tuple[VendorStep, ...] = Field(min_length=2)

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json"))

    def nodes(self) -> dict[str, list[VendorStep]]:
        """Steps grouped by the package node they serve. / 按服务的任务包节点分组的步骤。"""
        grouped: dict[str, list[VendorStep]] = {}
        for step in self.steps:
            grouped.setdefault(step.node, []).append(step)
        return grouped

    def wire(self) -> dict:
        """The task as sent in `flighttask_prepare`. / 在 `flighttask_prepare` 中发送的任务。"""
        return {"flight_id": self.flight_id, "task_type": self.task_type, "task_sha256": self.sha256,
                "frame": self.frame.model_dump(mode="json", exclude={"schema_version"}), "speed_mps": self.speed_mps,
                "steps": [step.model_dump(mode="json", exclude={"schema_version", "node"}) for step in self.steps]}


def flight_id(mission_id: str, version: int, package_hash: str) -> str:
    """The vendor flight identity of one approved package. / 一个已批准任务包的厂商飞行身份。"""
    return str(uuid.uuid5(NAMESPACE_ID, f"{mission_id}:v{version}:{package_hash}"))


def compile_task(package: MissionPackage, registry, profile: VendorProfile, profile_sha256: str, *, robot_id: str,
                 dock_id: str) -> VendorTask:
    """Translate the package's framework and inspection nodes into vendor waypoints; anything else is refused.

    把任务包的框架与巡检节点翻译为厂商航点；其他节点一律拒绝。
    """
    steps: list[dict] = []
    home = registry.data["home"]["position"]
    speed = 2.0

    def add(kind: str, node: str, position, asset: str | None = None) -> None:
        steps.append({"index": len(steps), "kind": kind, "node": node,
                      "position": tuple(float(v) for v in position), "asset_id": asset})

    for node in package.nodes:
        params = node.params
        if node.skill_id == "skill.flight.takeoff":
            add("takeoff", node.task_id, (home[0], home[1], float(params["altitude_m_agl"])))
        elif node.skill_id == "skill.inspect.asset":
            route = registry.route(params["approach_route_id"])
            for point in route:
                add("waypoint", node.task_id, point)
            speed = float(params.get("speed_mps", speed))
            add("photo", node.task_id, route[-1], params["asset_id"])
        elif node.skill_id == "skill.flight.return_home":
            for point in registry.route(params["return_route_id"]):
                add("waypoint", node.task_id, point)
        elif node.skill_id == "skill.flight.land":
            site = registry.data["landing_sites"][params["landing_site_id"]]
            add("land", node.task_id, site["position"])
        else:
            raise VendorError(f"{node.skill_id} has no vendor waypoint form")
    if package.spatial_scope is None:
        raise VendorError("the package has no spatial scope")
    frame = package.spatial_scope.frame
    return VendorTask(profile_id=profile.profile_id, profile_sha256=profile_sha256,
                      flight_id=flight_id(package.mission_id, package.mission_version, package.package_hash),
                      mission_id=package.mission_id, mission_version=package.mission_version,
                      package_hash=package.package_hash, robot_id=robot_id, dock_id=dock_id,
                      frame=TaskFrame(frame_id=frame.frame_id, map_version=frame.map_version), speed_mps=speed,
                      steps=tuple(VendorStep.model_validate(step) for step in steps))


# ── envelope / 信封 ──


def transaction_id(flight: str, method: str) -> str:
    """One transaction per (flight, command): a resend after a lost reply or a restart reuses it.

    每个（飞行，命令）一个事务：丢失答复或重启之后的重发沿用同一事务 ID。
    """
    if method not in WHITELIST:
        raise VendorError(f"{method} is not a whitelisted command")
    return str(uuid.uuid5(NAMESPACE_ID, f"{flight}:{method}"))


def command(method: str, *, flight: str, gateway: str, data: dict, ts: str) -> dict:
    """A command envelope; only whitelisted methods can be built. / 命令信封；只能构造白名单方法。"""
    return {"kind": "service", "tid": transaction_id(flight, method), "bid": flight, "ts": ts, "gateway": gateway,
            "method": method, "data": data}


INBOUND_KINDS = {"hello", "service_reply", "event", "state"}


def check_inbound(message) -> dict:
    """Shape check of one message from the vendor; the content is still only data. / 厂商报文的形状检查；内容仍只是数据。"""
    if not isinstance(message, dict) or message.get("kind") not in INBOUND_KINDS:
        raise VendorError("unknown vendor message")
    kind = message["kind"]
    if kind == "hello":
        if not isinstance(message.get("boot_id"), str) or not isinstance(message.get("protocol"), str):
            raise VendorError("malformed hello")
        return message
    if not isinstance(message.get("ts"), str) or not isinstance(message.get("data"), dict):
        raise VendorError(f"malformed {kind}")
    if kind == "service_reply":
        if message.get("method") not in WHITELIST or not isinstance(message.get("tid"), str) or \
                not isinstance(message["data"].get("result"), int):
            raise VendorError("malformed reply")
    elif kind == "event":
        if not isinstance(message.get("seq"), int) or not isinstance(message.get("method"), str) or \
                not isinstance(message.get("bid"), str):
            raise VendorError("malformed event")
    elif not isinstance(message.get("seq"), int):
        raise VendorError("malformed state")
    return message
