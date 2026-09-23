"""Signed approvals: tampered, expired, unsigned, wrong-robot and wrong-key packages are all rejected (D030).

签名审批：篡改、过期、未签名、错机器人与错密钥的任务包全部被拒（D030）。
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta, timezone

import pytest

from drone_agent.contracts import MissionPackage
from drone_agent.runtime.robot_state import RobotAuthorityState
from drone_agent.runtime.signing import (
    STATEMENT_PREFIX,
    SignatureRejected,
    SigningKey,
    TrustStore,
    statement_bytes,
    verify_package,
)
from drone_agent.runtime.wire import decode, encode
from tests.contracts.factories import NOW, package

KEY = SigningKey.generate()
TRUST = TrustStore.from_entries([KEY.trust_entry()])


def signed(**changes) -> MissionPackage:
    pkg = package()
    pkg.approval = KEY.sign(pkg.approval.model_copy(update=changes))
    return pkg


def rejected_code(pkg, robot_id="uav_01", now=NOW, trust=TRUST) -> str:
    with pytest.raises(SignatureRejected) as caught:
        verify_package(pkg, trust, robot_id=robot_id, now=now)
    return caught.value.code


def test_a_signed_package_verifies_and_names_its_signer():
    assert verify_package(signed(), TRUST, robot_id="uav_01", now=NOW) == KEY.key_id
    assert KEY.key_id.startswith("ed25519:") and len(KEY.key_id) == len("ed25519:") + 16


def test_the_statement_is_domain_separated_and_excludes_the_signature():
    pkg = signed()
    raw = statement_bytes(pkg.approval)
    assert raw.startswith(STATEMENT_PREFIX)
    assert pkg.approval.signature.encode() not in raw and b"signer_key_id" not in raw
    assert pkg.package_hash.encode() in raw


def test_unsigned_package_is_rejected():
    assert rejected_code(package()) == "onboard.unsigned"
    no_approval = package(approved=False)
    assert rejected_code(no_approval) == "onboard.unsigned"


def test_untrusted_key_is_rejected():
    other = SigningKey.generate()
    pkg = package()
    pkg.approval = other.sign(pkg.approval)
    assert rejected_code(pkg) == "onboard.untrusted_signer"


def test_forged_key_id_with_a_foreign_signature_is_rejected():
    other = SigningKey.generate()
    pkg = package()
    forged = other.sign(pkg.approval)
    pkg.approval = forged.model_copy(update={"signer_key_id": KEY.key_id})
    assert rejected_code(pkg) == "onboard.signature_invalid"


@pytest.mark.parametrize("field,value", [
    ("allowed_robots", ["uav_01", "uav_02"]),
    ("expires_at", NOW + timedelta(days=30)),
    ("approver", "someone-else"),
    ("package_hash", "0" * 64),
])
def test_tampering_with_any_signed_approval_field_breaks_the_signature(field, value):
    pkg = signed()
    pkg.approval = pkg.approval.model_copy(update={field: value})
    assert rejected_code(pkg) == "onboard.signature_invalid"


def test_tampering_with_the_package_after_signing_breaks_the_statement():
    pkg = signed()
    pkg.nodes[0].params["altitude_m_agl"] = 29
    assert rejected_code(pkg) == "onboard.statement_mismatch"


def test_expired_and_wrong_robot_packages_are_unauthorized():
    assert rejected_code(signed(), now=NOW + timedelta(hours=3)) == "onboard.package_unauthorized"
    assert rejected_code(signed(), robot_id="uav_02") == "onboard.package_unauthorized"


def test_signature_survives_the_protobuf_round_trip_and_utc_normalisation(tmp_path):
    import runpy
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    generated = runpy.run_path(str(root / "scripts/generate_proto.py"))["generate"](tmp_path)
    import sys

    sys.path.insert(0, str(generated.parent))
    try:
        from drone.contracts.v1 import contracts_pb2
    finally:
        sys.path.remove(str(generated.parent))
    pkg = package()
    local = timezone(timedelta(hours=8))
    pkg.approval = KEY.sign(pkg.approval.model_copy(update={"approved_at": NOW.astimezone(local)}))
    wire = encode(pkg, contracts_pb2.MissionPackage())
    restored = MissionPackage.model_validate(decode(contracts_pb2.MissionPackage.FromString(wire.SerializeToString())))
    assert verify_package(restored, TRUST, robot_id="uav_01", now=NOW) == KEY.key_id


def test_trust_file_holds_public_keys_only(tmp_path):
    path = tmp_path / "trust.json"
    TrustStore.write(path, [KEY])
    text = path.read_text(encoding="utf-8")
    assert "PRIVATE" not in text and json.loads(text)["signers"][0]["key_id"] == KEY.key_id
    assert TrustStore.load(path).key_ids == [KEY.key_id]
    private = tmp_path / "signing.pem"
    KEY.save(private)
    assert SigningKey.load(private).key_id == KEY.key_id
    entry = KEY.trust_entry()
    entry["key_id"] = "ed25519:0000000000000000"
    with pytest.raises(ValueError):
        TrustStore.from_entries([entry])
    assert base64.b64decode(KEY.trust_entry()["public_key"]) == KEY.public_raw


def test_robot_state_rejects_version_rollback_and_keeps_epochs_monotonic(tmp_path):
    state = RobotAuthorityState(tmp_path / "authority.json", "uav_01")
    state.accept_version("m", 2, "a" * 64)
    state.accept_version("m", 2, "a" * 64)
    for version, digest in ((1, "b" * 64), (2, "c" * 64)):
        with pytest.raises(SignatureRejected) as caught:
            state.accept_version("m", version, digest)
        assert caught.value.code == "onboard.version_rollback"
    state.record_epoch(3)
    reloaded = RobotAuthorityState(tmp_path / "authority.json", "uav_01")
    assert reloaded.minimum_epoch() == 4 and reloaded.missions["m"]["version"] == 2
    with pytest.raises(ValueError):
        reloaded.record_epoch(2)
    with pytest.raises(ValueError):
        RobotAuthorityState(tmp_path / "authority.json", "uav_02")
