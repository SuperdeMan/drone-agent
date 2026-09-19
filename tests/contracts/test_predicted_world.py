"""D007: predicted facts never satisfy preconditions; truth facts only inside the judge; positions need covariance."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from drone_agent.contracts import FactSource, Frame, Position, WorldFact, WorldKind
from tests.contracts.factories import NOW


def fact(kind: WorldKind, source: FactSource = FactSource.SENSOR, **kw) -> WorldFact:
    base = dict(
        fact_id="f1",
        subject="asset_01",
        predicate="anomaly_candidate",
        value=True,
        timestamp=NOW,
        valid_until=NOW + timedelta(seconds=30),
        source=source,
        world_kind=kind,
    )
    base.update(kw)
    return WorldFact(**base)


def test_predicted_fact_is_never_a_precondition():
    f = fact(WorldKind.PREDICTED, source=FactSource.MODEL, source_version="worldfly-0.3")
    assert not f.usable_as_precondition(now=NOW)
    assert not f.usable_as_precondition(now=NOW, judge_process=True)


def test_truth_fact_only_usable_in_judge_process():
    f = fact(WorldKind.TRUTH, source=FactSource.SIM_TRUTH)
    assert not f.usable_as_precondition(now=NOW)
    assert f.usable_as_precondition(now=NOW, judge_process=True)


def test_sim_truth_source_cannot_masquerade_as_belief():
    with pytest.raises(ValidationError):
        fact(WorldKind.BELIEF, source=FactSource.SIM_TRUTH)


def test_belief_fact_expires():
    f = fact(WorldKind.BELIEF)
    assert f.usable_as_precondition(now=NOW)
    assert not f.usable_as_precondition(now=NOW + timedelta(seconds=31))


def test_model_facts_carry_version():
    with pytest.raises(ValidationError):
        fact(WorldKind.BELIEF, source=FactSource.MODEL)
    assert fact(WorldKind.BELIEF, source=FactSource.MODEL, source_version="vlm-2026.08").confidence == 1.0


def test_positioned_fact_requires_frame_and_covariance():
    frame = Frame(frame_id="campus_enu", map_version="v1")
    with pytest.raises(ValidationError):
        fact(WorldKind.BELIEF, position=Position(x=1, y=2, z=3))
    with pytest.raises(ValidationError):
        fact(WorldKind.BELIEF, frame=frame, position=Position(x=1, y=2, z=3))
    ok = fact(WorldKind.BELIEF, frame=frame, position=Position(x=1, y=2, z=3, covariance=(1, 0, 0, 0, 1, 0, 0, 0, 4)))
    assert ok.position is not None and ok.position.covariance is not None


def test_covariance_shape():
    with pytest.raises(ValidationError):
        Position(x=0, y=0, z=0, covariance=(1, 2, 3))
