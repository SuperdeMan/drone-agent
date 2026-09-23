"""Read-only planning tools over the scene registry and local records (WP-M2-03, D012, D034).

Every tool is a pure read: it takes a small argument object, reads the registry or a records file that
was opened read-only at startup, and returns plain data. The result is wrapped with its source, the
retrieval time and a SHA-256 of the data, so the planner prompt can present it as data and replay can
bind to it. Only tools named in configs/planner_tools.yaml are ever exposed.

基于场景登记表与本地记录的只读规划工具（WP-M2-03，D012，D034）。

每个工具都是纯读取：接收一个小的参数对象，读取登记表或启动时以只读方式打开的记录文件，返回普通
数据。结果附带来源、检索时间与数据的 SHA-256，规划提示据此把它呈现为数据，回放也据此绑定。只有
configs/planner_tools.yaml 中列出的工具才会被暴露。
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import yaml

from drone_agent.admission.airspace import SimulatedAirspaceProvider
from drone_agent.contracts import PlannerToolAllowlist, utcnow

_SCHEMAS = {
    "assets.lookup": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "volume_id": {"type": "string", "description": "only assets registered in this volume"},
            "asset_ids": {"type": "array", "items": {"type": "string"}, "description": "only these assets"},
        },
    },
    "map.query": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"volume_id": {"type": "string", "description": "one volume; omit for all registered volumes"}},
    },
    "weather.current": {"type": "object", "additionalProperties": False, "properties": {}},
    "airspace.status": {
        "type": "object",
        "additionalProperties": False,
        "required": ["volume_id"],
        "properties": {"volume_id": {"type": "string"}},
    },
    "missions.history": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"asset_ids": {"type": "array", "items": {"type": "string"}}},
    },
}


class ToolError(ValueError):
    """A tool could not answer; reported as data, never as an instruction. / 工具无法回答；以数据形式报告。"""


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ToolCatalog:
    """The allowlisted read-only tools bound to one scene and one records snapshot.

    与一个场景及一份记录快照绑定的白名单只读工具。
    """

    def __init__(self, root: Path, scene: Path, records: Path | None = None, *, clock=utcnow):
        self.allowlist = PlannerToolAllowlist.from_yaml(root / "configs/planner_tools.yaml")
        self.scene = yaml.safe_load(scene.read_text(encoding="utf-8"))
        self.scene_id = self.scene.get("registry_id", scene.stem)
        # Extra records (history, notes) are read once; the file is never written.
        # 额外记录（历史、备注）只读取一次；从不写回。
        self.records = json.loads(records.read_text(encoding="utf-8")) if records else {}
        self.clock = clock
        self._airspace = SimulatedAirspaceProvider()

    def definitions(self) -> list[dict]:
        """MCP tool definitions, only for allowlisted names with an implementation. / 仅列出有实现的白名单工具。"""
        out = []
        for tool in self.allowlist.tools:
            if tool.name not in _SCHEMAS:
                continue
            out.append({
                "name": tool.name,
                "description": tool.description,
                "inputSchema": _SCHEMAS[tool.name],
                "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                                "openWorldHint": False},
            })
        return out

    def call(self, name: str, arguments: dict | None) -> dict:
        """Run one tool and wrap its data with source, time and digest. / 执行一个工具并附上来源、时间与摘要。"""
        if name not in self.allowlist.names() or name not in _SCHEMAS:
            raise ToolError(f"tool {name} is not allowlisted")
        arguments = dict(arguments or {})
        schema = _SCHEMAS[name]
        unknown = set(arguments) - set(schema["properties"])
        if unknown:
            raise ToolError(f"unknown arguments {sorted(unknown)}")
        missing = [key for key in schema.get("required", []) if key not in arguments]
        if missing:
            raise ToolError(f"missing arguments {missing}")
        data = getattr(self, "_" + name.replace(".", "_"))(arguments)
        return {"tool": name, "source": f"{self.scene_id}:{name}", "retrieved_at": self.clock().isoformat(),
                "sha256": digest(data), "data": data}

    def _assets_lookup(self, args: dict) -> dict:
        wanted = set(args.get("asset_ids") or [])
        volume = args.get("volume_id")
        history = self._history()
        assets = []
        for asset_id, record in sorted(self.scene.get("assets", {}).items()):
            if wanted and asset_id not in wanted:
                continue
            if volume and record.get("volume") != volume:
                continue
            last = next((h for h in reversed(history) if h.get("asset_id") == asset_id), None)
            assets.append({
                "asset_id": asset_id,
                "kind": record.get("kind", ""),
                "description": record.get("description", ""),
                "volume_id": record.get("volume"),
                "position_enu_m": list(record.get("position", [])),
                "observation_route_id": record.get("observation_route"),
                "visual_signature": record.get("visual_signature"),
                "last_inspection": last,
            })
        return {"frame": copy.deepcopy(self.scene["frame"]), "assets": assets}

    def _map_query(self, args: dict) -> dict:
        volumes = self.scene.get("volumes", {})
        selected = {args["volume_id"]: volumes.get(args["volume_id"])} if "volume_id" in args else volumes
        return {
            "frame": copy.deepcopy(self.scene["frame"]),
            "volumes": [
                {"volume_id": vid, "registered": record is not None,
                 "description": (record or {}).get("description", ""),
                 "airspace_mode": (record or {}).get("airspace_mode"),
                 "bounds_enu_m": copy.deepcopy((record or {}).get("bounds"))}
                for vid, record in sorted(selected.items())
            ],
            "home": copy.deepcopy(self.scene.get("home")),
            "landing_sites": sorted(self.scene.get("landing_sites", {})),
        }

    def _weather_current(self, args: dict) -> dict:
        weather = dict(self.scene.get("site_records", {}).get("weather", {}))
        weather.update(self.records.get("weather", {}))
        return weather

    def _airspace_status(self, args: dict) -> dict:
        return self._airspace.query(args["volume_id"], now=self.clock()).model_dump(mode="json")

    def _history(self) -> list[dict]:
        return list(self.scene.get("site_records", {}).get("history", [])) + list(self.records.get("history", []))

    def _missions_history(self, args: dict) -> dict:
        wanted = set(args.get("asset_ids") or [])
        rows = [h for h in self._history() if not wanted or h.get("asset_id") in wanted]
        return {"records": rows[-20:]}
