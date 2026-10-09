"""Energy reachability picks conservative edges and never guesses (D042).

能源可达性选择保守边，从不猜测（D042）。
"""

import pytest
import yaml

from drone_agent.guardian.energy import EnergyModel, EnergySettings
from tests.guardian.m3 import ROOT

SCENE = yaml.safe_load((ROOT / "configs/scenarios/m3_campus_v3.yaml").read_text(encoding="utf-8"))
HOME, SITE_B = [0, 0, 0], [-3, 31, 0]
GREEN = [0, 28, 4]


def model():
    return EnergyModel(EnergySettings.from_registry(SCENE))


def fed(rate=0.002, start=0.9, seconds=20):
    energy = model()
    for step in range(seconds * 5 + 1):
        t = step * 0.2
        energy.observe(t, start - rate * t)
    return energy


def test_fitted_rate_never_goes_below_the_prior():
    slow = fed(rate=0.0005)
    assert slow.fitted_rate == pytest.approx(0.0005, rel=0.05)
    assert slow.conservative_rate == pytest.approx(0.002)
    fast = fed(rate=0.004)
    assert fast.conservative_rate == pytest.approx(0.004, rel=0.1)


def test_a_jump_restarts_the_window_but_keeps_the_learned_rate():
    energy = fed(rate=0.003)
    learned = energy.conservative_rate
    energy.observe(100.0, 0.30)  # An injected or swapped level, not consumption. / 注入或换电的电量水平，不是消耗。
    assert len(energy.samples) == 1 and energy.conservative_rate == pytest.approx(learned)


@pytest.mark.parametrize(
    ("battery", "rtl", "site", "due"),
    [(0.32, True, True, True), (0.29, False, True, True), (0.25, False, False, True), (0.60, True, True, False)],
)
def test_reachability_contexts_at_the_green_asset(battery, rtl, site, due):
    context = fed().context(GREEN, battery, 0.2, HOME, {"site_b": SITE_B})
    assert (context["rtl_reachable"], context["nearest_site_reachable"], context["return_due"]) == (rtl, site, due)
    if site:
        assert context["nearest_site"] == "site_b"


@pytest.mark.parametrize("battery", [None, float("nan")])
def test_unknown_battery_is_never_reachable(battery):
    context = fed().context(GREEN, battery, 0.2, HOME, {"site_b": SITE_B})
    assert context["rtl_reachable"] is False and context["nearest_site_reachable"] is False


def test_unknown_position_is_never_reachable():
    context = fed().context(None, 0.9, 0.2, HOME, {"site_b": SITE_B})
    assert context["rtl_reachable"] is False and context["nearest_site_reachable"] is False


def test_a_headwind_at_least_the_return_speed_makes_everything_unreachable():
    settings = EnergySettings.from_registry({"energy_model": {**SCENE["energy_model"], "headwind_mps": 5.0}})
    context = EnergyModel(settings).context(GREEN, 0.9, 0.2, HOME, {"site_b": SITE_B})
    assert context["rtl_reachable"] is False and context["nearest_site_reachable"] is False


@pytest.mark.parametrize("height", [10.0, -20.0])
def test_a_landing_site_off_the_ground_plane_refuses_the_model(height):
    # The descent ends on z = 0; a rooftop (10 m) or a lower site (-20 m) would be mis-timed, so the map is refused
    # instead (D075). / 下降终止于 z = 0；楼顶（10 m）或更低站点（-20 m）的下降时间会算错，因此拒绝该地图（D075）。
    sites = {**SCENE["landing_sites"], "roof": {"position": [5, 5, height], "radius_m": 2, "reserved_for": "uav_01"}}
    with pytest.raises(ValueError, match="roof"):
        EnergySettings.from_registry({**SCENE, "landing_sites": sites})


def test_every_committed_map_with_an_energy_model_keeps_its_landing_sites_on_the_ground_plane():
    for path in sorted((ROOT / "configs/scenarios").glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "energy_model" in data:
            EnergySettings.from_registry(data)


def test_the_landing_time_is_the_registered_descent_from_the_current_height():
    # Pins the arithmetic the D075 clean-up kept: horizontal leg against the headwind, the climb allowance, then the
    # descent from the current height plus the allowance at the landing speed.
    # 钉住 D075 整理后保持不变的算式：逆风下的水平段、爬升裕量，再按降落速度从当前高度加裕量下降。
    s = EnergySettings.from_registry(SCENE)
    expected = 28.0 / (s.return_speed_mps - s.headwind_mps) + s.climb_allowance_m / s.descent_speed_mps \
        + (4.0 + s.climb_allowance_m) / s.descent_speed_mps
    assert model().travel(GREEN, HOME, landing=True) == pytest.approx(expected)
    below = model().travel([0, 28, -0.3], HOME, landing=True)
    assert below == pytest.approx(expected - 4.0 / s.descent_speed_mps)
