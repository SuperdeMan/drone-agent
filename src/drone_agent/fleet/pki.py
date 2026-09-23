"""Per-deployment private CA and mTLS certificates for mission-service <-> robot links (D030).

The deployment script creates a CA, one server certificate for the mission service and one client
certificate per robot. A robot certificate carries its identity twice: `CN=<robot_id>` and the URI SAN
`drone-agent://robot/<robot_id>`; the service trusts only the URI SAN. Keys are ECDSA P-256 so that gRPC's
TLS stack accepts them; private keys are written owner-only and never leave the host that uses them.

任务服务与机器人链路的每部署私有 CA 与 mTLS 证书（D030）。

部署脚本生成一个 CA、一张任务服务的服务端证书，以及每台机器人一张客户端证书。机器人证书两处携带
身份：`CN=<robot_id>` 与 URI SAN `drone-agent://robot/<robot_id>`；服务只信任 URI SAN。密钥使用 ECDSA
P-256 以便 gRPC 的 TLS 栈接受；私钥仅属主可读写，从不离开使用它的主机。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROBOT_URI_PREFIX = "drone-agent://robot/"


@dataclass(frozen=True)
class Credential:
    """A private key with its certificate. / 私钥及其证书。"""

    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate

    @property
    def cert_pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)

    @property
    def key_pem(self) -> bytes:
        return self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption())

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.cert)


def fingerprint(cert: x509.Certificate) -> str:
    """SHA-256 of the DER certificate, recorded in deployment receipts. / DER 证书的 SHA-256，写入部署回执。"""
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "drone-agent"),
                      x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def new_ca(common_name: str = "drone-agent deployment CA", days: int = 90) -> Credential:
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(_name(common_name)).issuer_name(_name(common_name))
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                     key_encipherment=False, data_encipherment=False, key_agreement=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .sign(key, hashes.SHA256())
    )
    return Credential(key, cert)


def issue(ca: Credential, *, common_name: str, dns: tuple[str, ...] = (), ips: tuple[str, ...] = (),
          uris: tuple[str, ...] = (), server: bool = False, client: bool = False, days: int = 30) -> Credential:
    """Issue a leaf certificate for a server, a client or both. / 为服务端、客户端或两者签发叶证书。"""
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    names = [x509.DNSName(d) for d in dns] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    names += [x509.UniformResourceIdentifier(u) for u in uris]
    usages = ([ExtendedKeyUsageOID.SERVER_AUTH] if server else []) + ([ExtendedKeyUsageOID.CLIENT_AUTH] if client else [])
    builder = (
        x509.CertificateBuilder().subject_name(_name(common_name)).issuer_name(ca.cert.subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage(usages), critical=False)
    )
    if names:
        builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
    return Credential(key, builder.sign(ca.key, hashes.SHA256()))


def robot_credential(ca: Credential, robot_id: str) -> Credential:
    return issue(ca, common_name=robot_id, uris=(ROBOT_URI_PREFIX + robot_id,), client=True)


def service_credential(ca: Credential, *, dns: tuple[str, ...] = ("mission-service", "localhost"),
                       ips: tuple[str, ...] = ("127.0.0.1",)) -> Credential:
    return issue(ca, common_name="mission-service", dns=dns, ips=ips, server=True)


def robot_id_from_uri(uri: str) -> str | None:
    """The robot id in a `drone-agent://robot/<id>` URI SAN. / 从 URI SAN 中取出机器人 ID。"""
    if not uri.startswith(ROBOT_URI_PREFIX):
        return None
    robot_id = uri[len(ROBOT_URI_PREFIX):]
    return robot_id if robot_id and "/" not in robot_id else None


def write_credential(credential: Credential, cert_path: Path, key_path: Path | None = None) -> None:
    """Write the certificate, and the key owner-only. / 写入证书，私钥仅属主可读写。"""
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(credential.cert_pem)
    if key_path is not None:
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(credential.key_pem)
