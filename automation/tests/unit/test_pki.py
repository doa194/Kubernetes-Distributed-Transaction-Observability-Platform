"""Certificate reuse rules and a real TLS handshake with Python's strict verification."""

import datetime as dt
import socket
import ssl
import threading

from txplatform import pki

NOW = dt.datetime(2026, 9, 17, tzinfo=dt.UTC)


def _facts(days_left: int = 60, *, issued: bool = True, dns=("localhost",), ips=("127.0.0.1",)) -> pki.CertificateFacts:
    return pki.CertificateFacts(NOW + dt.timedelta(days=days_left), frozenset(dns), frozenset(ips), issued)


def test_missing_certificate_is_created():
    assert pki.decide(None, NOW, check_names=True) is pki.Action.CREATE


def test_valid_certificate_is_reused():
    assert pki.decide(_facts(), NOW, check_names=True) is pki.Action.REUSE


def test_certificate_close_to_expiry_is_renewed():
    assert pki.decide(_facts(days_left=13), NOW, check_names=True) is pki.Action.RENEW


def test_certificate_from_a_previous_ca_is_renewed():
    assert pki.decide(_facts(issued=False), NOW, check_names=True) is pki.Action.RENEW


def test_certificate_missing_required_names_is_renewed():
    assert pki.decide(_facts(ips=()), NOW, check_names=True) is pki.Action.RENEW


def test_ca_names_are_not_checked():
    assert pki.decide(_facts(dns=(), ips=()), NOW, check_names=False) is pki.Action.REUSE


def test_second_ensure_reuses_existing_material(tmp_path):
    files = pki.PkiFiles(tmp_path / "ca.crt", tmp_path / "ca.key", tmp_path / "tls.crt", tmp_path / "tls.key")

    first = pki.ensure(files)
    leaf_before = files.tls_cert.read_bytes()
    second = pki.ensure(files)

    assert (first.ca_action, first.leaf_action) == (pki.Action.CREATE, pki.Action.CREATE)
    assert (second.ca_action, second.leaf_action) == (pki.Action.REUSE, pki.Action.REUSE)
    assert files.tls_cert.read_bytes() == leaf_before


def test_generated_chain_passes_strict_tls_verification(tmp_path):
    files = pki.PkiFiles(tmp_path / "ca.crt", tmp_path / "ca.key", tmp_path / "tls.crt", tmp_path / "tls.key")
    pki.ensure(files)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(files.tls_cert, files.tls_key)
    client_context = ssl.create_default_context(cafile=str(files.ca_cert))

    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]

        def serve():
            connection, _ = listener.accept()
            with server_context.wrap_socket(connection, server_side=True) as tls:
                tls.send(b"y")

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with socket.create_connection(("127.0.0.1", port)) as raw:
            with client_context.wrap_socket(raw, server_hostname="localhost") as tls:
                peer_names = tls.getpeercert()["subjectAltName"]
                # Waiting for the server's byte keeps the connection open until its handshake finished.
                assert tls.recv(1) == b"y"
        thread.join(timeout=5)

    assert peer_names == (("DNS", "localhost"), ("IP Address", "127.0.0.1"))
