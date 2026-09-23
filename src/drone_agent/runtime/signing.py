"""Approval signatures and onboard verification (WP-M2-11, D030).

The mission service signs one statement per approved version: the ApprovalRecord without its signature
fields, as canonical JSON with UTC timestamps, after the domain prefix `drone-agent/approval-statement/v1\\n`.
The statement contains the package hash, so one Ed25519 signature binds both the executable content and
the authorization. Robots hold only public keys, loaded once from a read-only trust file at start; they
never rotate keys in flight. Verification maps every failure to a controlled `onboard.*` code, and it
does not replace the registry and capability re-checks that run afterwards.

审批签名与机载验签（WP-M2-11，D030）。

任务服务对每个获批版本签署一条陈述：去掉签名字段的 ApprovalRecord，以 UTC 时间的规范 JSON 形式，
前加域分隔前缀 `drone-agent/approval-statement/v1\\n`。陈述包含任务包哈希，因此一个 Ed25519 签名同时
绑定可执行内容与授权。机器人只持有公钥，启动时从只读信任文件加载一次，飞行中从不轮换。验签把每种
失败映射为受控的 `onboard.*` 码，且不替代随后执行的登记表与能力复核。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from drone_agent.contracts import ApprovalRecord, MissionPackage

STATEMENT_PREFIX = b"drone-agent/approval-statement/v1\n"
TRUST_FORMAT = "drone.trust/v1"
PURPOSE = "mission_approval"


class SignatureRejected(ValueError):
    """Onboard acceptance failed; `code` is a controlled onboard.* issue code.

    机载接受失败；`code` 是受控的 onboard.* 问题码。
    """

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def statement_bytes(approval: ApprovalRecord) -> bytes:
    """The exact bytes that are signed. / 被签名的确切字节。"""
    data = approval.model_dump(mode="json", exclude={"signature", "signer_key_id"})
    # Timestamps travel as UTC Timestamps on the wire; the statement must survive that round trip.
    # 时间戳在线路上以 UTC Timestamp 传输；陈述必须在该往返后保持一致。
    data["approved_at"], data["expires_at"] = _utc(approval.approved_at), _utc(approval.expires_at)
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return STATEMENT_PREFIX + canonical.encode("utf-8")


def key_id_for(public_raw: bytes) -> str:
    return "ed25519:" + hashlib.sha256(public_raw).hexdigest()[:16]


def _raw(public: Ed25519PublicKey) -> bytes:
    return public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


@dataclass(frozen=True)
class SigningKey:
    """The mission service's approval signing key; only the service process ever holds it.

    任务服务的审批签名密钥；只有服务进程持有它。
    """

    private: Ed25519PrivateKey

    @classmethod
    def generate(cls) -> SigningKey:
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def load(cls, path: Path) -> SigningKey:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("approval signing key must be Ed25519")
        return cls(key)

    def save(self, path: Path) -> None:
        """Write PKCS#8 PEM with owner-only permissions. / 以仅属主可读写的权限写入 PKCS#8 PEM。"""
        data = self.private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption())
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)

    @property
    def public_raw(self) -> bytes:
        return _raw(self.private.public_key())

    @property
    def key_id(self) -> str:
        return key_id_for(self.public_raw)

    def sign(self, approval: ApprovalRecord) -> ApprovalRecord:
        """A copy of the approval carrying this key's signature. / 带有本密钥签名的审批副本。"""
        unsigned = approval.model_copy(update={"signature": None, "signer_key_id": None})
        signature = self.private.sign(statement_bytes(unsigned))
        return ApprovalRecord.model_validate({**unsigned.model_dump(), "signature": base64.b64encode(signature).decode(),
                                              "signer_key_id": self.key_id})

    def trust_entry(self) -> dict:
        return {"key_id": self.key_id, "public_key": base64.b64encode(self.public_raw).decode(), "purpose": PURPOSE}


class TrustStore:
    """Public keys a robot accepts approvals from, loaded once at process start.

    机器人接受其审批的公钥集合，进程启动时加载一次。
    """

    def __init__(self, signers: dict[str, Ed25519PublicKey]):
        self._signers = dict(signers)

    @classmethod
    def from_entries(cls, entries: list[dict]) -> TrustStore:
        signers = {}
        for entry in entries:
            if entry.get("purpose") != PURPOSE:
                continue
            raw = base64.b64decode(entry["public_key"], validate=True)
            if key_id_for(raw) != entry["key_id"]:
                raise ValueError("trust entry key id does not match its public key")
            signers[entry["key_id"]] = Ed25519PublicKey.from_public_bytes(raw)
        if not signers:
            raise ValueError("a trust store needs at least one approval signer")
        return cls(signers)

    @classmethod
    def load(cls, path: Path) -> TrustStore:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") != TRUST_FORMAT:
            raise ValueError("unsupported trust file format")
        return cls.from_entries(data.get("signers", []))

    @staticmethod
    def write(path: Path, keys: list[SigningKey]) -> None:
        """Write a trust file with public keys only. / 写入只含公钥的信任文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"format": TRUST_FORMAT, "signers": [k.trust_entry() for k in keys]}, indent=2),
                        encoding="utf-8")

    @property
    def key_ids(self) -> list[str]:
        return sorted(self._signers)

    def verify_approval(self, approval: ApprovalRecord) -> None:
        if not approval.signature or not approval.signer_key_id:
            raise SignatureRejected("onboard.unsigned", "the approval carries no signature")
        public = self._signers.get(approval.signer_key_id)
        if public is None:
            raise SignatureRejected("onboard.untrusted_signer", f"{approval.signer_key_id} is not trusted onboard")
        try:
            public.verify(base64.b64decode(approval.signature, validate=True), statement_bytes(approval))
        except (InvalidSignature, ValueError) as error:
            raise SignatureRejected("onboard.signature_invalid", "the approval signature does not verify") from error


def verify_package(package: MissionPackage, trust: TrustStore, *, robot_id: str, now: datetime) -> str:
    """Verify signature, statement binding and authorization; returns the signer key id.

    校验签名、陈述绑定与授权；返回签名密钥 ID。
    """
    approval = package.approval
    if approval is None:
        raise SignatureRejected("onboard.unsigned", "the package carries no approval")
    trust.verify_approval(approval)
    computed = package.compute_hash()
    if (approval.package_hash != computed or package.package_hash != computed
            or (approval.mission_id, approval.mission_version) != (package.mission_id, package.mission_version)):
        raise SignatureRejected("onboard.statement_mismatch", "the signed statement does not describe this package")
    if not package.is_authorized(robot_id=robot_id, now=now):
        raise SignatureRejected("onboard.package_unauthorized", "the approval does not authorize this robot now")
    return approval.signer_key_id
