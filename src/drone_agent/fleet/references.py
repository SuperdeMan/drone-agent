"""References to other projects, sites, robots or docks in a mission request, checked before planning (D082).

The service builds a name table from the operations catalog: every project, site, robot and dock id, each site map's
registry id, and the project and site aliases in `configs/sites/aliases/<catalog_id>.yaml` beside the catalog. A request
whose text names something outside its mission's binding is rejected before the model is called
(`scope.foreign_reference`), so a reference to another site is never answered with a look-alike asset of this one.

规划前检查任务请求中对其他项目、站点、机器人或机场的指代（D082）。

服务按运营目录建立名称表：全部项目、站点、机器人与机场 id，各站点地图的登记表 id，以及目录旁
`configs/sites/aliases/<catalog_id>.yaml` 中的项目与站点别名。请求文字点名了本任务绑定以外的对象，就在调用模型之前被拒绝
（`scope.foreign_reference`），对别的站点的指代不会被本站的相似资产应答。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

ALIASES_FORMAT = "drone.catalog-aliases/v1"
IDENTIFIER = "A-Za-z0-9_"


def compile_name(name: str) -> re.Pattern:
    """A name's pattern. ASCII ends keep identifier boundaries; a word of two or more characters ignores case while a
    lone letter keeps the case it is registered with ("site a few metres" is not site A); a space matches any whitespace
    or none; Chinese matches as written.

    名称的匹配式。ASCII 两端保持标识符边界；两个字符以上的词不分大小写，单独的字母保持登记时的大小写（"site a few metres"
    不是 site A）；空格匹配任意空白或无空白；中文按原样匹配。
    """
    tokens = name.split()
    if not tokens:
        raise ValueError("an empty name")
    parts = []
    for token in tokens:
        if re.fullmatch(r"[A-Za-z]", token) or not re.search(r"[A-Za-z]", token):
            parts.append(re.escape(token))
        else:
            parts.append(f"(?i:{re.escape(token)})")
    start = rf"(?<![{IDENTIFIER}])" if re.match(rf"[{IDENTIFIER}]", name) else ""
    end = rf"(?![{IDENTIFIER}])" if re.search(rf"[{IDENTIFIER}]$", name) else ""
    return re.compile(start + r"\s*".join(parts) + end)


@dataclass(frozen=True)
class Name:
    """One way to name a catalog entity. / 目录实体的一种称呼。"""

    kind: str  # project, site, robot or dock / 项目、站点、机器人或机场
    target: str
    text: str
    pattern: re.Pattern


def load_aliases(catalog_path: Path | None, catalog) -> tuple[dict[str, dict[str, list[str]]], str | None]:
    """The catalog's alias file and its digest; without one only ids are checked.

    目录的别名文件及其摘要；没有别名文件时只按 id 检查。
    """
    empty = {"projects": {}, "sites": {}}
    if catalog_path is None:
        return empty, None
    path = Path(catalog_path).parent / "aliases" / f"{catalog.catalog_id}.yaml"
    if not path.is_file():
        return empty, None
    raw = path.read_bytes()
    data = yaml.safe_load(raw.decode("utf-8")) or {}
    if data.get("format") != ALIASES_FORMAT or data.get("catalog_id") != catalog.catalog_id:
        raise ValueError(f"{path.name}: not the {ALIASES_FORMAT} aliases of {catalog.catalog_id}")
    found = {"projects": data.get("projects") or {}, "sites": data.get("sites") or {}}
    for section, known in (("projects", catalog.projects), ("sites", catalog.sites)):
        for target, names in found[section].items():
            if target not in known:
                raise ValueError(f"{path.name}: {section} has no {target}")
            if not isinstance(names, list) or not all(isinstance(n, str) and n.strip() for n in names):
                raise ValueError(f"{path.name}: {target} needs a list of names")
    return found, hashlib.sha256(raw).hexdigest()


class ReferenceIndex:
    """Every name of the catalog's projects, sites, robots and docks, and which of them a binding owns.

    目录中项目、站点、机器人与机场的全部称呼，以及其中哪些属于某个绑定。
    """

    def __init__(self, names: list[Name], dock_sites: dict[str, str], aliases_sha256: str | None = None):
        self.names, self.dock_sites, self.aliases_sha256 = names, dock_sites, aliases_sha256
        owners: dict[str, tuple[str, str]] = {}
        for name in names:
            key = name.text.casefold()
            if owners.setdefault(key, (name.kind, name.target)) != (name.kind, name.target):
                raise ValueError(f"the name {name.text!r} belongs to {owners[key]} and to {(name.kind, name.target)}")

    @classmethod
    def build(cls, catalog, registries: dict, catalog_path: Path | None = None) -> ReferenceIndex:
        aliases, digest = load_aliases(catalog_path, catalog)
        names: list[Name] = []

        def add(kind: str, target: str, text: str) -> None:
            if all((n.kind, n.target, n.text) != (kind, target, text) for n in names):
                names.append(Name(kind, target, text, compile_name(text)))

        for project_id in catalog.projects:
            for text in (project_id, *aliases["projects"].get(project_id, [])):
                add("project", project_id, text)
        for site_id in catalog.sites:
            registry_id = registries[site_id].data.get("registry_id") if site_id in registries else None
            for text in (site_id, *([registry_id] if registry_id else []), *aliases["sites"].get(site_id, [])):
                add("site", site_id, text)
        for robot_id in catalog.robots:
            add("robot", robot_id, robot_id)
        for dock_id in catalog.docks:
            add("dock", dock_id, dock_id)
        return cls(names, {d: dock.site_id for d, dock in catalog.docks.items()}, digest)

    def owned(self, name: Name, binding: dict[str, str]) -> bool:
        if name.kind == "project":
            return name.target == binding["project_id"]
        if name.kind == "site":
            return name.target == binding["site_id"]
        if name.kind == "robot":
            return name.target == binding["robot_id"]
        return self.dock_sites.get(name.target) == binding["site_id"]

    def foreign(self, text: str, binding: dict[str, str]) -> list[dict[str, str]]:
        """The names in `text` that belong to something outside `binding`, in table order.

        `text` 中属于 `binding` 以外对象的称呼，按名称表顺序。
        """
        hits = []
        for name in self.names:
            if self.owned(name, binding):
                continue
            found = name.pattern.search(text)
            if found:
                hits.append({"kind": name.kind, "id": name.target, "name": name.text, "matched": found.group(0)})
        return hits
