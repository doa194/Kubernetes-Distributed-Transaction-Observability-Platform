"""Local certificate authority and the TLS certificate for the platform's public endpoints.

Kong (https://localhost:8443) and Keycloak (https://localhost:9443) both present a
certificate issued by a CA generated on this workstation. The files live in the
git-ignored `.local/pki` folder and are reused while valid, so repeated bootstraps do
not rotate certificates (which would force pods to restart).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from txplatform import paths

CA_VALIDITY = dt.timedelta(days=365)
LEAF_VALIDITY = dt.timedelta(days=90)
# Renew before expiry so a long-running local environment never serves an expired certificate.
RENEW_BEFORE = dt.timedelta(days=14)
REQUIRED_DNS_NAMES = ("localhost",)
REQUIRED_IP_ADDRESSES = ("127.0.0.1",)


class Action(StrEnum):
    REUSE = "reuse"
    CREATE = "create"
    RENEW = "renew"


@dataclass(frozen=True)
class CertificateFacts:
    """What we need to know about an existing certificate to decide whether to keep it."""

    not_after: dt.datetime
    dns_names: frozenset[str]
    ip_addresses: frozenset[str]
    issued_by_current_ca: bool


def decide(facts: CertificateFacts | None, now: dt.datetime, *, check_names: bool) -> Action:
    """Pure decision: reuse a certificate, create it, or renew it."""
    if facts is None:
        return Action.CREATE
    if facts.not_after - now < RENEW_BEFORE:
        return Action.RENEW
    if not facts.issued_by_current_ca:
        return Action.RENEW
    if check_names and (
        not set(REQUIRED_DNS_NAMES) <= facts.dns_names or not set(REQUIRED_IP_ADDRESSES) <= facts.ip_addresses
    ):
        return Action.RENEW
    return Action.REUSE


@dataclass(frozen=True)
class PkiFiles:
    ca_cert: Path
    ca_key: Path
    tls_cert: Path
    tls_key: Path

    @staticmethod
    def default() -> PkiFiles:
        root = paths.local_state_dir() / "pki"
        return PkiFiles(root / "ca.crt", root / "ca.key", root / "tls.crt", root / "tls.key")


@dataclass(frozen=True)
class PkiResult:
    files: PkiFiles
    ca_action: Action
    leaf_action: Action


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _write_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )


def _facts(cert: x509.Certificate, ca_cert: x509.Certificate | None) -> CertificateFacts:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        dns = frozenset(san.get_values_for_type(x509.DNSName))
        ips = frozenset(str(ip) for ip in san.get_values_for_type(x509.IPAddress))
    except x509.ExtensionNotFound:
        dns, ips = frozenset(), frozenset()
    issued = True
    if ca_cert is not None:
        try:
            cert.verify_directly_issued_by(ca_cert)
        except (ValueError, TypeError, Exception):  # noqa: BLE001 - any verification failure means "not ours"
            issued = False
    return CertificateFacts(cert.not_valid_after_utc, dns, ips, issued)


def _load_cert(path: Path) -> x509.Certificate | None:
    return x509.load_pem_x509_certificate(path.read_bytes()) if path.is_file() else None


def _create_ca(files: PkiFiles) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = _new_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "txplatform local development CA")])
    now = _now()
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + CA_VALIDITY)
        # Python 3.13+ verifies strictly, so the CA must carry these extensions exactly.
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ski, critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski), critical=False)
        .sign(key, hashes.SHA256())
    )
    files.ca_key.parent.mkdir(parents=True, exist_ok=True)
    _write_key(files.ca_key, key)
    files.ca_cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert, key


def _create_leaf(files: PkiFiles, ca_cert: x509.Certificate, ca_key: rsa.RSAPrivateKey) -> None:
    key = _new_key()
    now = _now()
    sans = [x509.DNSName(n) for n in REQUIRED_DNS_NAMES] + [
        x509.IPAddress(ipaddress.ip_address(ip)) for ip in REQUIRED_IP_ADDRESSES
    ]
    ca_ski = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + LEAF_VALIDITY)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=True, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(files.tls_key, key)
    files.tls_cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def ensure(files: PkiFiles | None = None) -> PkiResult:
    """Creates or renews the CA and server certificate only when needed."""
    files = files or PkiFiles.default()
    now = _now()

    ca_cert = _load_cert(files.ca_cert)
    ca_action = decide(_facts(ca_cert, None) if ca_cert and files.ca_key.is_file() else None, now, check_names=False)
    if ca_action is Action.REUSE:
        ca_key = serialization.load_pem_private_key(files.ca_key.read_bytes(), password=None)
    else:
        ca_cert, ca_key = _create_ca(files)

    leaf_cert = _load_cert(files.tls_cert)
    leaf_facts = _facts(leaf_cert, ca_cert) if leaf_cert and files.tls_key.is_file() else None
    leaf_action = decide(leaf_facts, now, check_names=True)
    if leaf_action is not Action.REUSE:
        _create_leaf(files, ca_cert, ca_key)  # type: ignore[arg-type]
    return PkiResult(files, ca_action, leaf_action)
