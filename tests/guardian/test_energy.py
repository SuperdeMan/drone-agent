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
