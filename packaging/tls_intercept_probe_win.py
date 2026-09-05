"""Windows CI probe — the intervals.icu TLS path must agree with Windows.

Reproduces the 3.11.3 Windows report — rider log:
    EVENT=icu_oauth_network error=[SSL: CERTIFICATE_VERIFY_FAILED]
    certificate verify failed: invalid CA certificate (_ssl.c:1010)
and proves the v3.11.4 fix, on a real Windows machine:

1. Build an interceptor-style root Windows accepts but OpenSSL does not:
   X.509v3, keyUsage WITHOUT keyCertSign, no basicConstraints. Install it in
   the machine ROOT store (certutil; the CI runner is admin).
2. Serve HTTPS on 127.0.0.1 with a leaf signed by that root.
3. A: the 3.11.3 OpenSSL context (certifi + Windows store) must FAIL with
      "invalid CA certificate" — the report, reproduced.
   B: tls_trust.make_context() must be os-native and must SUCCEED.
   C: the same context must REJECT a leaf from a root NOT in the store.
   D: ...and REJECT a hostname mismatch.
   E: ...and reach the real https://intervals.icu (public CA): HTTP 401/403,
      no TLS error.
Exit 1 on any deviation. Run:  pip install cryptography && python packaging/tls_intercept_probe_win.py
"""
from __future__ import annotations

import datetime as dt
import http.server
import ipaddress
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID  # noqa: E402

import tls_trust  # noqa: E402

TRUSTED_CN = "Domestique CI interceptor root (keyUsage without keyCertSign)"
UNTRUSTED_CN = "Domestique CI untrusted root"
NOW = dt.datetime.now(dt.timezone.utc)
_KU_SERVER = dict(digital_signature=True, content_commitment=False, key_encipherment=True,
                  data_encipherment=False, key_agreement=False, key_cert_sign=False,
                  crl_sign=False, encipher_only=False, decipher_only=False)


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _root(cn, key):
    """Interceptor-style root: v3, keyUsage without keyCertSign, no basicConstraints.
    Windows trusts it by identity once installed; OpenSSL's X509_check_ca() → 0."""
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    return (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(NOW - dt.timedelta(days=1))
            .not_valid_after(NOW + dt.timedelta(days=3650))
            .add_extension(x509.KeyUsage(**_KU_SERVER), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))


def _leaf(root, root_key, key, sans):
    return (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .issuer_name(root.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(NOW - dt.timedelta(days=1))
            .not_valid_after(NOW + dt.timedelta(days=300))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(**_KU_SERVER), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False)
            .sign(root_key, hashes.SHA256()))


class _Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def _serve(cert_pem: Path, key_pem: Path) -> int:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(cert_pem, key_pem)
    srv.socket = sctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def _get(ctx, url):
    try:
        return httpx.get(url, verify=ctx, timeout=20), None
    except httpx.TransportError as e:
        return None, e


def main() -> int:
    if sys.platform != "win32":
        print("tls_intercept_probe_win.py: Windows only (certutil + CryptoAPI)")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="dq-tls-"))
    trusted_key, untrusted_key, leaf_key = _key(), _key(), _key()
    trusted = _root(TRUSTED_CN, trusted_key)
    untrusted = _root(UNTRUSTED_CN, untrusted_key)
    sans_ok = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    sans_bad = [x509.DNSName("nothere.invalid")]

    def dump(name, cert):
        p = tmp / f"{name}.pem"
        p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return p

    keyp = tmp / "leaf.key"
    keyp.write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM,
                                            serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
    root_cer = tmp / "root.cer"
    root_cer.write_bytes(trusted.public_bytes(serialization.Encoding.DER))
    subprocess.run(["certutil", "-addstore", "-f", "Root", str(root_cer)], check=True)

    failures: list[str] = []

    def ok(label):
        print(f"PROBE {label}: OK")

    def bad(label, why):
        failures.append(f"{label}: {why}")
        print(f"PROBE {label}: FAIL — {why}")

    try:
        p_trusted = _serve(dump("leaf_trusted", _leaf(trusted, trusted_key, leaf_key, sans_ok)), keyp)
        p_untrusted = _serve(dump("leaf_untrusted", _leaf(untrusted, untrusted_key, leaf_key, sans_ok)), keyp)
        p_badname = _serve(dump("leaf_badname", _leaf(trusted, trusted_key, leaf_key, sans_bad)), keyp)

        # A — the 3.11.3 context reproduces the rider's error on real Windows.
        r, e = _get(tls_trust._openssl_context(), f"https://127.0.0.1:{p_trusted}/")
        if e is None:
            bad("A openssl refuses OS-trusted interceptor root", f"unexpectedly accepted (HTTP {r.status_code})")
        elif "invalid CA certificate" not in str(e):
            bad("A openssl refuses OS-trusted interceptor root", f"failed for a different reason: {e}")
        else:
            ok("A openssl reproduces 'invalid CA certificate' against the OS-trusted interceptor root")

        # B — the shipped context asks Windows and agrees with the browser.
        ctx = tls_trust.make_context()
        backend = tls_trust.backend_of(ctx)
        if backend != tls_trust.OS_NATIVE:
            bad("B backend", f"make_context() gave '{backend}', expected os-native (truststore missing?)")
        r, e = _get(ctx, f"https://127.0.0.1:{p_trusted}/")
        if e is not None or r.status_code != 200:
            bad("B os-native accepts OS-trusted interceptor root", str(e) if e else f"HTTP {r.status_code}")
        else:
            ok("B os-native accepts the OS-trusted interceptor root")

        # C — not CERT_NONE in disguise.
        r, e = _get(ctx, f"https://127.0.0.1:{p_untrusted}/")
        if e is None:
            bad("C os-native rejects untrusted root", f"accepted (HTTP {r.status_code})")
        else:
            ok(f"C os-native rejects an untrusted root ({e})")

        # D — hostname still checked.
        r, e = _get(ctx, f"https://127.0.0.1:{p_badname}/")
        if e is None:
            bad("D os-native rejects hostname mismatch", f"accepted (HTTP {r.status_code})")
        else:
            ok(f"D os-native rejects a hostname mismatch ({e})")

        # E — the public-CA path through the same verifier.
        r, e = _get(ctx, "https://intervals.icu/api/v1/athlete/0")
        if e is not None:
            bad("E os-native reaches intervals.icu", str(e))
        elif r.status_code not in (401, 403):
            bad("E os-native reaches intervals.icu", f"HTTP {r.status_code}")
        else:
            ok(f"E intervals.icu reachable through os-native verification (HTTP {r.status_code})")
    finally:
        subprocess.run(["certutil", "-delstore", "Root", TRUSTED_CN], check=False)

    if failures:
        print("TLS probe FAILED:\n  " + "\n  ".join(failures))
        return 1
    print("TLS probe: all probes passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
