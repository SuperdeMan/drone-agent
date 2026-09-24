"""AirspaceConstraintProvider: the regulatory admission hook (D016, WP-M2-06, WP-M3-19).

M2 shipped a stub that never claims a filing. M3 defines the real interface — airspace class, the UOM flight-activity
filing (identifier, state, approved window and volume), the aircraft's real-name registration and Remote ID — and a
recorded backend that replays versioned UOM responses, so admission rules are testable without a live connection.
The live UOM client is connected before real flights in M4-A (D016); until then nothing here talks to a network.

Admission accepts a volume only when the scene explicitly declares `airspace_mode: simulation`. A volume declared
`real` needs an approved filing that is valid now, covers the mission window and the volume, a registration that
matches the aircraft and, where required, an active Remote ID; every missing or unknown item fails closed with its
own code. Any other or missing mode is undeclared and also fails closed.

AirspaceConstraintProvider：法规准入接入点（D016，WP-M2-06，WP-M3-19）。

M2 只有从不声称已报备的桩。M3 定义真实接口——空域类别、UOM 飞行活动申请（编号、状态、批准时段与空域）、航空器
实名登记与 Remote ID——并提供回放版本化 UOM 应答的录制后端，使准入规则无需实时连接即可测试。真实 UOM 客户端在
M4-A 真机前接入（D016）；在此之前本模块不访问任何网络。

只有场景显式声明 `airspace_mode: simulation` 的体积才准入。声明为 `real` 的体积需要：当前有效且已批准的申请，
覆盖任务时段与该体积；与该航空器一致的实名登记；要求时 Remote ID 处于开启状态。任何一项缺失或未知都以各自的
码 fail closed。其他或缺失的模式视为未声明，同样 fail closed。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import AwareDatetime, Field

from drone_agent.contracts.common import ContractModel
from drone_agent.runtime.issues import Issue, issue

FILING_STATES = ("draft", "submitted", "approved", "rejected", "expired", "cancelled", "unknown")


class AirspaceStatus(ContractModel):
    """What the provider knows about one volume and one aircraft. / provider 对某个体积与某架航空器的已知信息。"""

    volume_id: str
    airspace_class: str
    filing_status: str = Field(description="filed only for an approved filing valid now / 仅当批准且当前有效时为 filed")
    remote_id_required: bool
    source: str
    checked_at: datetime
    # M3 (WP-M3-19): filing and identity details; absent means unknown, never granted.
    # M3（WP-M3-19）：申请与身份细节；缺席即未知，绝不视为许可。
    filing_id: str | None = None
    filing_state: str = "unknown"
    approved_volume_id: str | None = None
    approved_from: AwareDatetime | None = None
    approved_until: AwareDatetime | None = None
    registration_id: str | None = None
    remote_id_active: bool | None = None


class AirspaceConstraintProvider(Protocol):
    def query(self, volume_id: str, *, now: datetime) -> AirspaceStatus:  # pragma: no cover - protocol
        ...


class SimulatedAirspaceProvider:
    """The M2 stub: answers `simulated_unfiled` for every volume and never invents a filing.

    M2 桩实现：对任何体积都回答 `simulated_unfiled`，从不虚构报备。
    """

    source = "stub:m2-simulated"

    def query(self, volume_id: str, *, now: datetime) -> AirspaceStatus:
        return AirspaceStatus(
            volume_id=volume_id,
            airspace_class="unknown",
            filing_status="simulated_unfiled",
            remote_id_required=True,
            source=self.source,
            checked_at=now,
        )


class RecordedUomProvider:
    """Replays recorded UOM answers from `<directory>/<volume_id>.json`; a missing recording is unknown.

    The recording holds the airspace class, the filing and the aircraft's registration and Remote ID exactly as the
    UOM returned them. `filing_status` is derived here, never copied: it is `filed` only for an approved filing whose
    window contains `now`.

    从 `<directory>/<volume_id>.json` 回放录制的 UOM 应答；没有录制即为未知。录制内容是 UOM 原样返回的空域类别、申请、
    航空器实名登记与 Remote ID。`filing_status` 在这里推导而不是照抄：只有已批准且时段包含 `now` 的申请才是 `filed`。
    """

    def __init__(self, directory: Path):
        self.directory = directory

    def query(self, volume_id: str, *, now: datetime) -> AirspaceStatus:
        path = self.directory / f"{volume_id}.json"
        if not path.is_file():
            return AirspaceStatus(volume_id=volume_id, airspace_class="unknown", filing_status="unknown",
                                  remote_id_required=True, source=f"recorded:missing:{self.directory.name}",
                                  checked_at=now)
        record = json.loads(path.read_text(encoding="utf-8"))
        filing = record.get("filing") or {}
        state = filing.get("state", "unknown")
        if state not in FILING_STATES:
            state = "unknown"
        approved_from = datetime.fromisoformat(filing["approved_from"]) if filing.get("approved_from") else None
        approved_until = datetime.fromisoformat(filing["approved_until"]) if filing.get("approved_until") else None
        valid_now = bool(approved_from and approved_until and approved_from <= now < approved_until)
        summary = "filed" if state == "approved" and valid_now else (
            "expired" if state == "approved" else state)
        remote = record.get("remote_id") or {}
        return AirspaceStatus(
            volume_id=volume_id,
            airspace_class=record.get("airspace_class", "unknown"),
            filing_status=summary,
            remote_id_required=bool(remote.get("required", True)),
            source=f"recorded:{record.get('recording_id', path.stem)}",
            checked_at=now,
            filing_id=filing.get("filing_id"),
            filing_state=state,
            approved_volume_id=filing.get("volume_id"),
            approved_from=approved_from,
            approved_until=approved_until,
            registration_id=(record.get("registration") or {}).get("registration_id"),
            remote_id_active=remote.get("active"),
        )


def airspace_issues(volume_id: str, volume: dict | None, status: AirspaceStatus, *,
                    window: tuple[datetime, datetime] | None = None,
                    registration_id: str | None = None) -> list[Issue]:
    """Regulatory admission: simulation must be declared by the scene; real mode needs a complete, matching filing.

    法规准入：仿真模式必须由场景声明；真实模式需要完整且匹配的申请。
    """
    if volume is None:
        return [issue("scope.unregistered_volume", f"volume {volume_id} is not registered", affected=[volume_id])]
    mode = volume.get("airspace_mode")
    if mode == "simulation":
        return []
    if mode != "real":
        return [issue("airspace.mode_undeclared", f"{volume_id} does not declare simulation or real airspace mode",
                      affected=[volume_id])]
    if status.filing_status in ("unknown", ""):
        return [issue("airspace.unknown", f"airspace status for {volume_id} is unknown", affected=[volume_id])]
    if status.filing_status != "filed":
        return [issue("airspace.not_filed", f"{volume_id} requires a UOM filing; status {status.filing_status}",
                      affected=[volume_id])]
    found: list[Issue] = []
    if status.approved_volume_id != volume_id:
        found.append(issue("airspace.filing_scope", f"filing {status.filing_id} does not cover {volume_id}",
                           affected=[volume_id]))
    if window is not None and not (
        status.approved_from and status.approved_until
        and status.approved_from <= window[0] and window[1] <= status.approved_until
    ):
        found.append(issue("airspace.filing_window", f"filing {status.filing_id} does not cover the mission window",
                           affected=[volume_id]))
    if not registration_id or status.registration_id != registration_id:
        found.append(issue("airspace.registration_mismatch", "the filing is not for this registered aircraft",
                           affected=[volume_id]))
    if status.remote_id_required and status.remote_id_active is not True:
        found.append(issue("airspace.remote_id_inactive", "Remote ID is required but not confirmed active",
                           affected=[volume_id]))
    return found
