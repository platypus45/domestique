"""TLS trust for the app's outbound HTTPS (intervals.icu, GitHub).

Two verifiers, chosen by platform:

* Windows → the operating system's own verifier (CryptoAPI, via truststore).
  Antivirus "HTTPS scanning" and corporate proxies re-sign every connection
  with a root that lives in the Windows store. Some of those roots carry no CA
  markers at all (no basicConstraints, no keyUsage) or only an EKU. Windows
  accepts such a root — verified on a Windows runner against SChannel itself
  (packaging/tls_intercept_probe_win.py) — while OpenSSL applies
  X509_check_ca() to the trust anchor and refuses it with "invalid CA
  certificate"; no verify flag relaxes that. Windows is not blind either: a
  root with basicConstraints CA:FALSE or a keyUsage without keyCertSign is
  refused by BOTH verifiers (a browser on that PC would fail too, so no rider
  has one). v3.11.3 loaded the Windows store into OpenSSL and only moved the
  rider's failure from "unable to get local issuer certificate" to "invalid
  CA certificate". Asking Windows is the only way to agree with the browser.
* macOS / Linux → OpenSSL with certifi + the OS store (unchanged since 3.11.3).

Every outbound intervals.icu/GitHub connection — httpx (OAuth, update check)
AND urllib (sync, wellness, FIT upload/download, calendar push) — takes its
context from make_context(); a call site left on the stock context is the
3.11.3 bug again (pinned by tests/test_tls_trust.py).

No app imports here: the Windows CI probe imports this module on its own.
"""
from __future__ import annotations

import ssl
import sys

OS_NATIVE = "os-native"
OPENSSL = "openssl"


def _openssl_context() -> ssl.SSLContext:
    """certifi + the OS trust store, verified by OpenSSL (the 3.11.3 context)."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    try:
        ctx.load_default_certs()  # Windows CA/ROOT stores, macOS keychain, Linux dirs
    except Exception:
        pass
    return ctx


def _os_native_context() -> ssl.SSLContext:
    """The OS verifier's verdict (truststore). ImportError if truststore is absent."""
    import truststore
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # truststore consults these only AFTER the OS store said no — a fallback
    # for a Windows install whose root auto-update is disabled, never a bypass.
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    return ctx


_OPENSSL_CTX: ssl.SSLContext | None = None


def make_context(platform: str | None = None) -> ssl.SSLContext:
    """A verifying client context for the given (default: current) platform.

    Windows: a NEW truststore context per call, never shared. truststore
    0.10.4 flips its inner context to CERT_NONE for the duration of each
    handshake and restores it without holding its lock
    (sethmlarson/truststore#209); two overlapping handshakes on one shared
    context can leave it at CERT_NONE for the rest of the process, after which
    CryptoAPI is told to accept any certificate. A context per call has
    nothing to share; construction costs about a millisecond.
    ponytail: cache the Windows context again once truststore fixes #209.
    macOS/Linux: one cached OpenSSL context (thread-safe).
    """
    global _OPENSSL_CTX
    if (platform or sys.platform) == "win32":
        try:
            return _os_native_context()
        except Exception:
            pass  # truststore missing/broken: OpenSSL + OS store still beats nothing
    if _OPENSSL_CTX is None:
        _OPENSSL_CTX = _openssl_context()
    return _OPENSSL_CTX


def backend_name(platform: str | None = None) -> str:
    """Which verifier make_context() hands out, without building one (diag, logs)."""
    if (platform or sys.platform) == "win32":
        try:
            import truststore  # noqa: F401
            return OS_NATIVE
        except Exception:
            return OPENSSL
    return OPENSSL


def backend_of(ctx: ssl.SSLContext) -> str:
    """'os-native' for a truststore context, 'openssl' otherwise (diag + logs)."""
    return OS_NATIVE if type(ctx).__module__.startswith("truststore") else OPENSSL


def peer_chain_summary(host: str, port: int = 443, timeout: float = 5.0) -> list[str]:
    """Subject/issuer of every certificate the server presents, fetched WITHOUT
    verification. Diagnostics only — logged when a TLS failure is reported so
    the log names the interceptor (antivirus / proxy) instead of us guessing.
    Never used to trust anything."""
    import socket
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as raw, \
            ctx.wrap_socket(raw, server_hostname=host) as s:
        chain = s._sslobj.get_unverified_chain() or ()   # CPython 3.10+; public in 3.13
    out = []
    for c in chain:
        if isinstance(c, (bytes, bytearray)):            # 3.13+: DER bytes
            import hashlib
            out.append("sha256=" + hashlib.sha256(c).hexdigest()[:16])
            continue
        info = c.get_info()
        out.append(f"subject={_rdn(info.get('subject'))} issuer={_rdn(info.get('issuer'))}")
    return out


def _rdn(name) -> str:
    """(((k, v),), ...) → 'CN=x,O=y'."""
    short = {"commonName": "CN", "organizationName": "O", "organizationalUnitName": "OU",
             "countryName": "C", "stateOrProvinceName": "ST", "localityName": "L"}
    parts = []
    for rdn in name or ():
        for k, v in rdn:
            parts.append(f"{short.get(k, k)}={v}")
    return ",".join(parts) or "?"
