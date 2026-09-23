"""Robot-level authority state that must survive across mission versions (D030, D032).

Two facts outlive a single mission process: the highest control epoch ever granted on this robot and the
highest mission version accepted per mission. A new version must use a higher epoch and a mission may
never roll back to an older version. The file is written atomically with fsync; an unreadable file is a
failure, never a reset to zero.

需要跨任务版本保存的机器人级控制权状态（D030，D032）。

有两件事比单个任务进程活得更久：本机器人授予过的最高控制权代次，以及每个任务已接受的最高版本。新版本
必须使用更高代次，任务永远不能回退到旧版本。文件以 fsync 原子写入；文件不可读是失败，绝不重置为零。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from drone_agent.runtime.signing import SignatureRejected

STATE_FORMAT = "drone.robot-authority/v1"


class RobotAuthorityState:
    """Epoch watermark and accepted mission versions for one robot. / 单台机器人的代次水位与已接受任务版本。"""

    def __init__(self, path: Path, robot_id: str):
        self.path, self.robot_id = path, robot_id
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("format") != STATE_FORMAT or data.get("robot_id") != robot_id:
                raise ValueError("robot authority state belongs to another robot or format")
            self.epoch_watermark = int(data["epoch_watermark"])
            self.missions = {k: dict(v) for k, v in data["missions"].items()}
        else:
            self.epoch_watermark, self.missions = -1, {}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_name(self.path.name + ".pending")
        with pending.open("wb") as stream:
            stream.write(json.dumps({"format": STATE_FORMAT, "robot_id": self.robot_id,
                                     "epoch_watermark": self.epoch_watermark, "missions": self.missions},
                                    sort_keys=True).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(self.path)

    def accept_version(self, mission_id: str, version: int, package_hash: str) -> None:
        """Record a newer version; the same version is accepted again only with the same package.

        记录更新的版本；同一版本只有任务包相同时才能再次接受。
        """
        known = self.missions.get(mission_id)
        if known is not None:
            if version < known["version"] or (version == known["version"] and package_hash != known["package_hash"]):
                raise SignatureRejected("onboard.version_rollback",
                                        f"{mission_id} v{version} would replace accepted v{known['version']}")
            if version == known["version"]:
                return
        self.missions[mission_id] = {"version": version, "package_hash": package_hash}
        self._write()

    def minimum_epoch(self) -> int:
        """The lowest epoch a new lease may carry. / 新租约允许的最低代次。"""
        return self.epoch_watermark + 1

    def record_epoch(self, epoch: int) -> None:
        if epoch < self.epoch_watermark:
            raise ValueError("epoch watermark cannot move backwards")
        if epoch > self.epoch_watermark:
            self.epoch_watermark = epoch
            self._write()
