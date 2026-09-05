"""TLS trust for intervals.icu — the Windows rider's second log (v3.11.3).

v3.11.3 loaded the OS store into OpenSSL; the rider's error moved from
"unable to get local issuer certificate" to "invalid CA certificate": the
antivirus root was now FOUND and then refused, because OpenSSL applies
X509_check_ca() to trust anchors and an interceptor's root is often not a
well-formed CA. Windows trusts a root by identity. v3.11.4 asks Windows.

Fixtures (tests/fixtures/tls, TEST-ONLY keys, valid to 2126): one leaf for
localhost signed by four roots — well-formed, keyUsage without keyCertSign,
no CA markers at all, basicConstraints CA:FALSE.
"""
from __future__ import annotations

import socket
import ssl
import sys
import threading
from pathlib import Path

import pytest

import tls_trust

FIX = Path(__file__).parent / "fixtures" / "tls"
INTERCEPTOR_ROOTS = ["ku_no_certsign", "no_ca_markers", "ca_false"]


def _serve(cert: Path, key: Path) -> int:
    """One-shot TLS server on an ephemeral port; returns the port."""
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(cert, key)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        try:
            conn, _ = srv.accept()
            with sctx.wrap_socket(conn, server_side=True) as s:
                s.recv(1)
        except Exception:
            pass
        finally:
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def _handshake(ctx: ssl.SSLContext, port: int):
    """None when the client accepted the server, else the ssl error raised."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with ctx.wrap_socket(raw, server_hostname="localhost") as s:
                s.send(b"x")
        return None
    except ssl.SSLError as e:
        return e


# ── the failure class, pinned ────────────────────────────────────────────────

@pytest.mark.parametrize("root", INTERCEPTOR_ROOTS)
def test_openssl_refuses_interceptor_style_root_even_when_trusted(root):
    # Exactly the rider's line: trusted root present, still "invalid CA certificate".
    ctx = ssl.create_default_context(cafile=str(FIX / f"root_{root}.pem"))
    err = _handshake(ctx, _serve(FIX / f"leaf_{root}.pem", FIX / "leaf.key"))
    assert err is not None, "OpenSSL accepted a root without CA markers — Windows special-case may be obsolete"
    assert err.verify_message == "invalid CA certificate"


def test_openssl_accepts_well_formed_root():
    ctx = ssl.create_default_context(cafile=str(FIX / "root_good.pem"))
    assert _handshake(ctx, _serve(FIX / "leaf_good.pem", FIX / "leaf.key")) is None


# ── platform selection ───────────────────────────────────────────────────────

@pytest.mark.parametrize("plat", ["darwin", "linux"])
def test_non_windows_uses_openssl_with_certifi_and_os_store(plat):
    ctx = tls_trust.make_context(plat)
    assert tls_trust.backend_of(ctx) == tls_trust.OPENSSL
    assert type(ctx) is ssl.SSLContext
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert ctx.cert_store_stats()["x509_ca"] > 100   # certifi loaded


def test_windows_uses_os_native_verifier():
    import truststore
    ctx = tls_trust.make_context("win32")
    assert tls_trust.backend_of(ctx) == tls_trust.OS_NATIVE
    assert type(ctx) is truststore.SSLContext
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_os_native_context_still_rejects_an_untrusted_root():
    # Not CERT_NONE in disguise: a root the OS has never seen must fail.
    # (macOS/Linux backend here; the Windows backend is proved in CI —
    # packaging/tls_intercept_probe_win.py.)
    ctx = tls_trust.make_context("win32")
    assert tls_trust.backend_of(ctx) == tls_trust.OS_NATIVE   # not the fallback
    err = _handshake(ctx, _serve(FIX / "leaf_good.pem", FIX / "leaf.key"))
    assert err is not None


def test_windows_without_truststore_falls_back_to_openssl(monkeypatch):
    monkeypatch.setitem(sys.modules, "truststore", None)   # import → ImportError
    ctx = tls_trust.make_context("win32")
    assert tls_trust.backend_of(ctx) == tls_trust.OPENSSL
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_current_platform_default_matches_explicit():
    assert tls_trust.backend_of(tls_trust.make_context()) == \
        tls_trust.backend_of(tls_trust.make_context(sys.platform))


# ── app wiring ───────────────────────────────────────────────────────────────

def test_app_caches_one_context_from_tls_trust():
    import app as app_module
    ctx = app_module._icu_verify()
    assert app_module._icu_verify() is ctx
    assert tls_trust.backend_of(ctx) == tls_trust.backend_of(tls_trust.make_context())


# ── diagnostics: the log names the interceptor ───────────────────────────────

def test_peer_chain_summary_names_subject_and_issuer():
    port = _serve(FIX / "leaf_no_ca_markers.pem", FIX / "leaf.key")
    out = tls_trust.peer_chain_summary("127.0.0.1", port, timeout=5)
    assert len(out) == 1
    assert "subject=CN=localhost" in out[0]
    assert "issuer=CN=Domestique TEST-ONLY root no_ca_markers" in out[0]


def test_oauth_network_failure_logs_peer_chain(monkeypatch, caplog):
    """A TLS refusal on the token POST must leave the interceptor's identity in
    the log — the whole reason two rider logs went nowhere."""
    import logging
    import httpx
    import app as app_module
    from fastapi.testclient import TestClient

    def boom(*a, **k):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] invalid CA certificate")
    monkeypatch.setattr(httpx, "post", boom)
    monkeypatch.setattr(tls_trust, "peer_chain_summary",
                        lambda host, port=443, timeout=5.0: ["subject=CN=intervals.icu issuer=CN=Some AV Root"])
    app_module._icu_oauth_states["st-test"] = {"profile_id": "p", "ts": 9e12, "return_to": "/"}
    monkeypatch.setattr(app_module, "_icu_profile_exists", lambda pm, pid: True)
    client = TestClient(app_module.app)
    with caplog.at_level(logging.WARNING, logger="app"):
        r = client.get("/oauth/icu/callback", params={"code": "x", "state": "st-test"},
                       follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "reason=network" in r.headers["location"]
    assert any("icu_oauth_peer_chain" in m and "Some AV Root" in m for m in caplog.messages), caplog.messages
