"""AirspaceConstraintProvider: the regulatory admission hook (D016, WP-M2-06).

M2 only has a stub. It never claims a filing: every query answers `simulated_unfiled`. Admission accepts
a volume only when the scene explicitly declares `airspace_mode: simulation`; a volume declared `real`
needs `filing_status == filed`, which the stub can never give, so it fails closed. Any other or missing
mode is undeclared and also fails closed. The real UOM interface is defined in M3 (WP-M3-19) and
connected before real flights in M4-A.

AirspaceConstraintProvider：法规准入接入点（D016，WP-M2-06）。

M2 只有桩实现，它从不声称已报备：每次查询都返回 `simulated_unfiled`。只有场景显式声明
`airspace_mode: simulation` 的体积才准入；声明为 `real` 的体积要求 `filing_status == filed`，桩永远给
不出，因此 fail closed。其他或缺失的模式视为未声明，同样 fail closed。真实 UOM 接口在 M3 定义
（WP-M3-19），在 M4-A 真机前接入。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from drone_agent.contracts.common import ContractModel
from drone_agent.runtime.issues import Issue, issue


class AirspaceStatus(ContractModel):
    """What the provider knows about one volume. / provider 对某个体积的已知信息。"""

    volume_id: str
    airspace_class: str
    filing_status: str
    remote_id_required: bool
    source: str
    checked_at: datetime


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


def airspace_issues(volume_id: str, volume: dict | None, status: AirspaceStatus) -> list[Issue]:
    """Regulatory admission: simulation must be declared by the scene; real mode needs a filing.

    法规准入：仿真模式必须由场景声明；真实模式需要报备。
    """
    if volume is None:
        return [issue("scope.unregistered_volume", f"volume {volume_id} is not registered", affected=[volume_id])]
    mode = volume.get("airspace_mode")
    if mode == "simulation":
        return []
    if mode == "real":
        if status.filing_status == "filed":
            return []
        if status.filing_status in ("unknown", ""):
            return [issue("airspace.unknown", f"airspace status for {volume_id} is unknown", affected=[volume_id])]
        return [issue("airspace.not_filed", f"{volume_id} requires a UOM filing; status {status.filing_status}",
                      affected=[volume_id])]
    return [issue("airspace.mode_undeclared", f"{volume_id} does not declare simulation or real airspace mode",
                  affected=[volume_id])]
