"""X1 ground cells in P3 airspace reservations (D088): a ground package's footprint is the box of its ground places in
`ground.` cells, which never conflict with `air.` cells, and its lost-contact envelope stays inside its drivable area.

X1 地面单元接入 P3 空域预约（D088）：地面任务包的航迹覆盖是其地面位置的外包矩形，用 `ground.` 单元表示，与 `air.` 单元
从不冲突；其失联包络留在可行驶区域之内。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from drone_agent.fleet.reservation import (
    GROUND_PLACES,
    GROUND_PREFIX,
    PREFIX,
    UnresolvedFootprint,
    envelope_cells,
    footprint,
    ground_points,
    ground_volume,
    is_cell,
    package_points,
)
from drone_agent.fleet.scheduling_models import load_scheduling
from tests.admission.ground_scene import ground, ground_package

ROOT = Path(__file__).resolve().parents[2]
GRID = load_scheduling(ROOT / "configs/scheduling/x1_campus_v1.yaml").airspace
ORIGIN = (2.0, -4.0)


def test_ground_places_match_the_ground_manifests():
    manifests = {yaml.safe_load(path.read_text(encoding="utf-8"))["skill_id"]
                 for path in (ROOT / "configs/skills/ground").glob("*.yaml")}
    assert manifests == set(GROUND_PLACES)


def test_a_ground_package_holds_ground_cells_around_every_place_it_drives():
    package = ground_package().model_dump(mode="json")
    data = ground().data
    points = ground_points(data, package)
    fp = footprint(GRID, ORIGIN, points, ground_volume(data), GROUND_PREFIX)
    assert fp.cells and all(cell.startswith("ground.campus.") and is_cell(cell) for cell in fp.cells)
    for x, y in points:
        sx, sy = ORIGIN[0] + x, ORIGIN[1] + y
        assert fp.box[0] < sx < fp.box[2] and fp.box[1] < sy < fp.box[3]
    # The face leg and every waypoint are among the places. / 正对段与每个航点都在其中。
    recheck = package["nodes"][2]["params"]
    assert tuple(recheck["face_point"]) in points and tuple(recheck["goal"]) in points


def test_ground_and_air_cells_over_the_same_ground_never_share_a_name():
    # Site a's pad and asset_mid, seen once from the air and once from the ground map. / 站点 a 的机位与 asset_mid，
    # 分别从空中地图与地面地图看。
    air = footprint(GRID, (0.0, 0.0), [(0.0, 0.0), (5.0, 4.0)], None)
    ground_fp = footprint(GRID, ORIGIN, [(-2.0, 4.0), (3.0, 8.0)], ground_volume(ground().data), GROUND_PREFIX)
    assert air.box == pytest.approx(ground_fp.box) and len(air.cells) == len(ground_fp.cells)
    assert not set(air.cells) & set(ground_fp.cells)
    assert {c.split(".")[0] + "." for c in air.cells} == {PREFIX}


def test_a_ground_envelope_stays_inside_the_drivable_area():
    data = ground().data
    fp = footprint(GRID, ORIGIN, ground_points(data, ground_package().model_dump(mode="json")), ground_volume(data),
                   GROUND_PREFIX)
    grown = envelope_cells(GRID, fp, 500.0, GROUND_PREFIX)
    drivable = ground_volume(data)["bounds"]
    assert set(fp.cells) <= set(grown) and all(c.startswith(GROUND_PREFIX) for c in grown)
    for cell in grown:
        _, _, i, j = cell.rsplit(".", 3)
        x, y = int(i) * GRID.cell_m, int(j) * GRID.cell_m
        assert x < ORIGIN[0] + drivable["x"][1] and x + GRID.cell_m > ORIGIN[0] + drivable["x"][0]
        assert y < ORIGIN[1] + drivable["y"][1] and y + GRID.cell_m > ORIGIN[1] + drivable["y"][0]


@pytest.mark.parametrize("mutate", [
    lambda nodes: nodes[1].update(skill_id="skill.ground.teleport"),
    lambda nodes: nodes[1]["params"].pop("waypoints"),
    lambda nodes: nodes[2]["params"].pop("face_point"),
], ids=["unknown_skill", "no_waypoints", "no_face_point"])
def test_an_unbounded_ground_package_is_never_reserved(mutate):
    package = ground_package().model_dump(mode="json")
    mutate(package["nodes"])
    with pytest.raises(UnresolvedFootprint):
        ground_points(ground().data, package)


def test_aircraft_places_never_resolve_ground_skills():
    package = ground_package().model_dump(mode="json")
    with pytest.raises(UnresolvedFootprint):
        package_points(ground().data, package)
