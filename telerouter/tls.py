"""Self-signed TLS: a local CA (kept) and a server certificate for this host's names and IPs.

Clients pin the CA (its SHA-256 is part of the install command), so the server certificate can be
re-issued (new IP, new name) without re-enrolling clients.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
import logging
import socket
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .store import write_private

log = logging.getLogger("telerouter.tls")


@dataclass
class TlsFiles:
    cert: str
    key: str
    ca_pem: bytes
    ca_sha256: str          # hex SHA-256 of the CA certificate (DER)
    server_spki: str        # base64 SHA-256 of the server public key (curl --pinnedpubkey)
    names: list[str]


def local_names(extra: list[str]) -> list[str]:
    names = {"localhost", "127.0.0.1", socket.gethostname()}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            names.add(info[4][0])
    except OSError:
        pass
    try:  # the address used for the default route
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))
        names.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    names.update(n for n in extra if n)
    return sorted(names)


def _san(names: list[str]) -> x509.SubjectAlternativeName:
    entries: list[x509.GeneralName] = []
    for n in names:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            entries.append(x509.DNSName(n))
    return x509.SubjectAlternativeName(entries)


def _spki_pin(cert: x509.Certificate) -> str:
    der = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(der).digest()).decode()


def ensure(data_dir: str, extra_names: list[str], own_cert: str = "", own_key: str = "") -> TlsFiles:
    d = Path(data_dir) / "tls"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    ca_key_p, ca_p = d / "ca.key", d / "ca.pem"
    key_p, cert_p, names_p = d / "server.key", d / "server.pem", d / "server.names"
    now = dt.datetime.now(dt.timezone.utc)

    if not (ca_key_p.exists() and ca_p.exists()):
        ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "telerouter local CA")])
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
              .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
              .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                           data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                           crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
              .sign(ca_key, hashes.SHA256()))
        write_private(ca_key_p, ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
        write_private(ca_p, ca.public_bytes(serialization.Encoding.PEM))
        log.info("created a local certificate authority")
    ca = x509.load_pem_x509_certificate(ca_p.read_bytes())
    ca_key = serialization.load_pem_private_key(ca_key_p.read_bytes(), None)
    ca_der = ca.public_bytes(serialization.Encoding.DER)
    ca_sha = hashlib.sha256(ca_der).hexdigest()

    if own_cert and own_key:
        cert = x509.load_pem_x509_certificate(Path(own_cert).read_bytes())
        return TlsFiles(own_cert, own_key, ca_p.read_bytes(), ca_sha, _spki_pin(cert), [])

    names = local_names(extra_names)
    current = names_p.read_text().split() if names_p.exists() else []
    renew = not (key_p.exists() and cert_p.exists()) or current != names
    if not renew:
        cert = x509.load_pem_x509_certificate(cert_p.read_bytes())
        renew = cert.not_valid_after_utc < now + dt.timedelta(days=30)
    if renew:
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "telerouter")]))
                .issuer_name(ca.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=397))
                .add_extension(_san(names), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                             data_encipherment=False, key_agreement=True, key_cert_sign=False,
                                             crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                .sign(ca_key, hashes.SHA256()))
        write_private(key_p, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        # chain: server + CA, so clients that trust the CA can verify
        write_private(cert_p, cert.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
        write_private(names_p, " ".join(names))
        log.info("issued a server certificate for %s", ", ".join(names))
    cert = x509.load_pem_x509_certificate(cert_p.read_bytes())
    return TlsFiles(str(cert_p), str(key_p), ca_p.read_bytes(), ca_sha, _spki_pin(cert), names)


def cert_sha256(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()
