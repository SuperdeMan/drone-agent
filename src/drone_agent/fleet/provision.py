"""Provision the per-deployment approval key, trust file, CA and mTLS certificates (D030, WP-M2-18).

Run once on the host that keeps them, inside the project's own `secrets/` directory; files are written owner-only
and never printed. The result lists only public facts (signer key id and certificate fingerprints) for the
deployment receipt. Re-running keeps an existing signing key and reissues TLS leaves only when they are missing or
expire within three days.

在保存它们的主机上、项目自己的 `secrets/` 目录中生成每部署的审批密钥、信任文件、CA 与 mTLS 证书（D030，
WP-M2-18）；文件仅属主可读写，从不打印。结果只列出公开事实（签名密钥 ID 与证书指纹）供部署回执使用。重复
运行保留已有签名密钥，仅在 TLS 叶证书缺失或三天内过期时重新签发。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from drone_agent.fleet import pki
from drone_agent.runtime.signing import SigningKey, TrustStore


def _private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


def _fresh(path: Path) -> bool:
    if not path.exists():
        return False
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    return cert.not_valid_after_utc - dt.datetime.now(dt.timezone.utc) > dt.timedelta(days=3)


def provision(output: Path, robot_id: str = "uav_01") -> dict:
    output.mkdir(parents=True, exist_ok=True)
    os.chmod(output, 0o700)
    signing = output / "approval-signing.key"
    created = []
    if not signing.exists():
        SigningKey.generate().save(signing)
        created.append("approval-signing.key")
    key = SigningKey.load(signing)
    trust = output / "trust" / "trust.json"
    TrustStore.write(trust, [key])
    ca_cert, ca_key = output / "ca" / "ca.crt", output / "ca" / "ca.key"
    if not _fresh(ca_cert) or not ca_key.exists():
        ca = pki.new_ca()
        _private(ca_key, ca.key_pem)
        ca_cert.write_bytes(ca.cert_pem)
        created.append("ca")
    ca = pki.Credential(serialization.load_pem_private_key(ca_key.read_bytes(), password=None),
                        x509.load_pem_x509_certificate(ca_cert.read_bytes()))
    leaves = {"service-tls": ("service", lambda: pki.service_credential(ca)),
              "robot-tls": ("robot", lambda: pki.robot_credential(ca, robot_id))}
    fingerprints = {"ca": ca.fingerprint}
    for folder, (name, issue) in leaves.items():
        cert = output / folder / f"{name}.crt"
        if not _fresh(cert) or "ca" in created:
            credential = issue()
            _private(output / folder / f"{name}.key", credential.key_pem)
            cert.write_bytes(credential.cert_pem)
            created.append(folder)
        (output / folder / "ca.crt").write_bytes(ca.cert_pem)
        fingerprints[name] = pki.fingerprint(x509.load_pem_x509_certificate(cert.read_bytes()))
    return {"signer_key_id": key.key_id, "fingerprints": fingerprints, "robot_id": robot_id, "created": created}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/secrets"))
    parser.add_argument("--robot-id", default="uav_01")
    args = parser.parse_args()
    print(json.dumps(provision(args.output, args.robot_id)))


if __name__ == "__main__":
    main()
