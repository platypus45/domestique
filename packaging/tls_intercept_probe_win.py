"""Windows CI probe — the intervals.icu TLS path must agree with Windows.

A Windows rider's 3.11.3 log:
    EVENT=icu_oauth_network error=[SSL: CERTIFICATE_VERIFY_FAILED]
    certificate verify failed: invalid CA certificate (_ssl.c:1010)
Browsers on the same PC work. Diagnosis: an antivirus/proxy root in the
Windows store that Windows trusts and OpenSSL refuses. v3.11.4 asks Windows
(truststore / CryptoAPI) on win32. This script proves both halves on a real
Windows runner, with an INDEPENDENT Windows oracle (PowerShell's
Invoke-WebRequest = .NET = SChannel) next to truststore.

Modes
  (default)       For each root shape: install it in the machine ROOT store,
                  serve HTTPS on 127.0.0.1 with a leaf signed by it, record
                  three verdicts: OpenSSL (the 3.11.3 context), truststore
                  (the shipped context) and SChannel (oracle). Gates:
                    A1 well-formed root: all three accept (environment sane)
                    A2 at least one shape Windows accepts and OpenSSL refuses
                       with "invalid CA certificate" (the diagnosis is real)
                    A3 truststore == SChannel verdict on every shape
                    A4 with a well-formed trusted root: an untrusted root and
                       a hostname mismatch are still refused (unmasked)
                    A5 the real https://intervals.icu answers 401/403 through
                       truststore (public-CA path)
                  Writes tls_gap_shape.txt (first A2 shape) for serve-fake-icu.
  serve-fake-icu  Interceptor of that shape for intervals.icu itself: root in
                  the ROOT store, hosts entry 127.0.0.1 intervals.icu, HTTPS on
                  :443 answering the token exchange like intervals.icu answers
                  a bad code (HTTP 400). Prints READY, serves forever.
  e2e-oauth       Drives the RUNNING frozen EXE (127.0.0.1:22400) through
                  sign-in start -> callback with a dummy code. Through the
                  interceptor the token POST must reach the fake intervals.icu
                  and come back HTTP 400 (reason=exchange&status=400); the
                  rider's failure would be reason=network. Also checks the app
                  log for tls=os-native and the exchange line.
Exit 1 on any deviation.
"""
from __future__ import annotations

import datetime as dt
import http.server
import ipaddress
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

import tls_trust  # noqa: E402

GAP_FILE = Path("tls_gap_shape.txt")
HOSTS = Path(r"C:\Windows\System32\drivers\etc\hosts")
FAKE_ICU_STATUS = 400
FAKE_ICU_BODY = {"error": "invalid_grant", "error_description": "Code not found"}

# Root shapes. `good` is the positive control; `ku_no_certsign` is the
# negative control (Windows refuses it too: "not valid for the requested
# usage", CI run 33950212553). The middle three are the candidate rider shapes.
SHAPES = ["good", "no_ca_markers", "ca_false", "eku_only", "ku_no_certsign"]


def _crypto():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    return x509, hashes, serialization, rsa, ExtendedKeyUsageOID, NameOID


def _ku(x509, **on):
    flags = dict(digital_signature=False, content_commitment=False, key_encipherment=False,
                 data_encipherment=False, key_agreement=False, key_cert_sign=False,
                 crl_sign=False, encipher_only=False, decipher_only=False)
    flags.update(on)
    return x509.KeyUsage(**flags)


def make_root(shape: str, cn: str):
    x509, hashes, _ser, rsa, EKU, NameOID = _crypto()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    b = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))
    if shape == "good":
        b = (b.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
              .add_extension(_ku(x509, digital_signature=True, key_cert_sign=True, crl_sign=True), critical=True))
    elif shape == "no_ca_markers":
        pass
    elif shape == "ca_false":
        b = b.add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=False)
    elif shape == "eku_only":
        b = b.add_extension(x509.ExtendedKeyUsage([EKU.SERVER_AUTH]), critical=False)
    elif shape == "ku_no_certsign":
        b = b.add_extension(_ku(x509, digital_signature=True, key_encipherment=True), critical=False)
    else:
        raise SystemExit(f"unknown shape {shape}")
    return b.sign(key, hashes.SHA256()), key


def make_leaf(root, root_key, sans):
    x509, hashes, _ser, rsa, EKU, NameOID = _crypto()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(sans[0].value))]))
            .issuer_name(root.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=300))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([EKU.SERVER_AUTH]), critical=False)
            .add_extension(_ku(x509, digital_signature=True, key_encipherment=True), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False)
            .sign(root_key, hashes.SHA256()))
    return cert, key


def write_chain(tmp: Path, name: str, leaf, leaf_key, root):
    """leaf + root PEM (interceptors send both) and the leaf key."""
    _x, _h, ser, *_ = _crypto()
    pem = tmp / f"{name}.pem"
    pem.write_bytes(leaf.public_bytes(ser.Encoding.PEM) + root.public_bytes(ser.Encoding.PEM))
    key = tmp / f"{name}.key"
    key.write_bytes(leaf_key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption()))
    return pem, key


def install_root(tmp: Path, root, cn: str):
    _x, _h, ser, *_ = _crypto()
    cer = tmp / (re.sub(r"[^A-Za-z0-9]+", "_", cn) + ".cer")
    cer.write_bytes(root.public_bytes(ser.Encoding.DER))
    subprocess.run(["certutil", "-addstore", "-f", "Root", str(cer)], check=True,
                   stdout=subprocess.DEVNULL)


def remove_root(cn: str):
    subprocess.run(["certutil", "-delstore", "Root", cn], check=False, stdout=subprocess.DEVNULL)


class _Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, ctype="text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, b"ok")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if self.path == "/api/oauth/token":
            self._send(FAKE_ICU_STATUS, json.dumps(FAKE_ICU_BODY).encode(), "application/json")
        else:
            self._send(404, b"not found")

    def log_message(self, *a):
        pass


class _QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # clients that refuse our certificate abort the handshake — expected


def serve(cert_pem: Path, key_pem: Path, host="127.0.0.1", port=0) -> int:
    srv = _QuietServer((host, port), _Handler)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(cert_pem, key_pem)
    srv.socket = sctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def verdict_httpx(ctx, url) -> str:
    try:
        r = httpx.get(url, verify=ctx, timeout=20)
        return "ok" if r.status_code == 200 else f"http {r.status_code}"
    except httpx.TransportError as e:
        return f"refused: {str(e)[:160]}"


def verdict_schannel(url) -> str:
    """Independent Windows oracle: PowerShell Invoke-WebRequest (.NET -> SChannel)."""
    cmd = (f"try {{ (Invoke-WebRequest -Uri '{url}' -UseBasicParsing -TimeoutSec 20).StatusCode }} "
           f"catch {{ $e = $_.Exception; while ($e.InnerException) {{ $e = $e.InnerException }}; 'ERR: ' + $e.Message }}")
    out = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", cmd],
                         capture_output=True, text=True, timeout=90).stdout.strip()
    return "ok" if out == "200" else f"refused: {out[:160]}"


def _ok(v: str) -> bool:
    return v == "ok"


# ── default mode ─────────────────────────────────────────────────────────────

def probe() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dq-tls-"))
    x509, *_ = _crypto()
    sans = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    failures: list[str] = []
    rows = {}

    def fail(label, why):
        failures.append(f"{label}: {why}")
        print(f"FAIL {label}: {why}")

    shipped = tls_trust.make_context()
    backend = tls_trust.backend_of(shipped)
    print(f"shipped context backend: {backend}")
    if backend != tls_trust.OS_NATIVE:
        fail("backend", f"make_context() gave '{backend}' on win32 (truststore missing?)")

    for shape in SHAPES:
        cn = f"Domestique CI interceptor root [{shape}]"
        root, root_key = make_root(shape, cn)
        leaf, leaf_key = make_leaf(root, root_key, sans)
        pem, key = write_chain(tmp, f"leaf_{shape}", leaf, leaf_key, root)
        install_root(tmp, root, cn)
        try:
            port = serve(pem, key)
            url = f"https://127.0.0.1:{port}/"
            # Fresh OpenSSL context AFTER the install: load_default_certs() snapshots
            # the Windows store, and the app builds its context at first use — long
            # after an antivirus installed its root. A context built before the
            # install says "self-signed certificate in certificate chain" (root not
            # found), which is not the rider's failure.
            rows[shape] = {"openssl": verdict_httpx(tls_trust._openssl_context(), url),
                           "truststore": verdict_httpx(shipped, url),
                           "schannel": verdict_schannel(url)}
        finally:
            remove_root(cn)
        r = rows[shape]
        print(f"{shape:15s} openssl={r['openssl']}\n{'':15s} truststore={r['truststore']}\n{'':15s} schannel={r['schannel']}")

    # A1 — sane environment: a well-formed trusted root satisfies everyone.
    g = rows["good"]
    if not (_ok(g["openssl"]) and _ok(g["truststore"]) and _ok(g["schannel"])):
        fail("A1 well-formed root accepted by all", json.dumps(g))
    else:
        print("A1 OK: well-formed trusted root accepted by OpenSSL, truststore and SChannel")

    # A2 — the diagnosis: a Windows-accepted, OpenSSL-refused root shape exists.
    gaps = [s for s in SHAPES if _ok(rows[s]["schannel"]) and _ok(rows[s]["truststore"])
            and "invalid CA certificate" in rows[s]["openssl"]]
    if not gaps:
        fail("A2 gap shape exists", "no root shape that Windows accepts and OpenSSL refuses "
                                    "with 'invalid CA certificate' — the diagnosis is unproven")
    else:
        print(f"A2 OK: Windows accepts / OpenSSL refuses ('invalid CA certificate'): {gaps}")
        GAP_FILE.write_text(gaps[0])

    # A3 — the shipped context agrees with Windows on every shape.
    for s in SHAPES:
        if _ok(rows[s]["truststore"]) != _ok(rows[s]["schannel"]):
            fail("A3 truststore agrees with SChannel", f"{s}: truststore={rows[s]['truststore']} schannel={rows[s]['schannel']}")
    if not any(f.startswith("A3") for f in failures):
        print("A3 OK: truststore verdict == SChannel verdict on every shape")

    # A4 — still a verifier: untrusted root and hostname mismatch refused, with a
    # WELL-FORMED trusted root so nothing else masks the result.
    cn = "Domestique CI well-formed root [A4]"
    root, root_key = make_root("good", cn)
    stranger, stranger_key = make_root("good", "Domestique CI untrusted root [A4]")
    install_root(tmp, root, cn)
    try:
        ok_port = serve(*write_chain(tmp, "a4_ok", *make_leaf(root, root_key, sans), root))
        bad_port = serve(*write_chain(tmp, "a4_badname", *make_leaf(root, root_key, [x509.DNSName("nothere.invalid")]), root))
        unt_port = serve(*write_chain(tmp, "a4_untrusted", *make_leaf(stranger, stranger_key, sans), stranger))
        for label, port, want_ok in (("trusted+matching", ok_port, True),
                                     ("hostname mismatch", bad_port, False),
                                     ("untrusted root", unt_port, False)):
            url = f"https://127.0.0.1:{port}/"
            t, s_ = verdict_httpx(shipped, url), verdict_schannel(url)
            print(f"A4 {label:18s} truststore={t}\n{'':21s} schannel={s_}")
            if _ok(t) != want_ok:
                fail(f"A4 {label}", f"truststore={t}")
            if _ok(s_) != want_ok:
                fail(f"A4 {label} (oracle)", f"schannel={s_}")
    finally:
        remove_root(cn)

    # A5 — the public-CA path through the same verifier.
    try:
        r = httpx.get("https://intervals.icu/api/v1/athlete/0", verify=shipped, timeout=20)
        if r.status_code in (401, 403):
            print(f"A5 OK: intervals.icu reachable through truststore (HTTP {r.status_code})")
        else:
            fail("A5 intervals.icu", f"HTTP {r.status_code}")
    except httpx.TransportError as e:
        fail("A5 intervals.icu", str(e)[:200])

    if failures:
        print("TLS probe FAILED:\n  " + "\n  ".join(failures))
        return 1
    print("TLS probe: all gates passed")
    return 0


# ── serve-fake-icu ───────────────────────────────────────────────────────────

def serve_fake_icu() -> int:
    shape = GAP_FILE.read_text().strip() if GAP_FILE.exists() else "no_ca_markers"
    tmp = Path(tempfile.mkdtemp(prefix="dq-fake-icu-"))
    x509, *_ = _crypto()
    cn = f"Domestique CI antivirus root [{shape}]"
    root, root_key = make_root(shape, cn)
    leaf, leaf_key = make_leaf(root, root_key, [x509.DNSName("intervals.icu")])
    pem, key = write_chain(tmp, "fake_icu", leaf, leaf_key, root)
    install_root(tmp, root, cn)
    with HOSTS.open("a", encoding="ascii") as h:
        h.write("\r\n127.0.0.1 intervals.icu\r\n")
    subprocess.run(["ipconfig", "/flushdns"], check=False, stdout=subprocess.DEVNULL)
    srv = _QuietServer(("127.0.0.1", 443), _Handler)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(pem, key)
    srv.socket = sctx.wrap_socket(srv.socket, server_side=True)
    print(f"READY shape={shape} root_cn={cn!r} hosts=127.0.0.1 intervals.icu port=443", flush=True)
    srv.serve_forever()
    return 0


# ── e2e-oauth ────────────────────────────────────────────────────────────────

def e2e_oauth(base="http://127.0.0.1:22400") -> int:
    failures: list[str] = []

    def fail(label, why):
        failures.append(f"{label}: {why}")
        print(f"FAIL {label}: {why}")

    with httpx.Client(base_url=base, timeout=60, follow_redirects=False) as c:
        hb = c.get("/api/diag/health").json()
        tlsb = (hb.get("checks", {}).get("icu_oauth") or {}).get("tls_backend")
        print(f"diag tls_backend={tlsb}")
        if tlsb != tls_trust.OS_NATIVE:
            fail("frozen EXE tls_backend", f"{tlsb!r} (expected os-native)")
        r = c.get("/oauth/icu/start", params={"return_to": "/"})
        loc = r.headers.get("location", "")
        print(f"start -> {r.status_code} {loc[:120]}")
        state = (parse_qs(urlparse(loc).query).get("state") or [""])[0]
        if r.status_code not in (302, 303, 307) or not state:
            fail("oauth start", f"status={r.status_code} location={loc[:200]}")
            print("\n".join(failures)); return 1
        r = c.get("/oauth/icu/callback", params={"code": "ci-dummy-code", "state": state})
        loc = r.headers.get("location", "")
        print(f"callback -> {r.status_code} {loc}")
        if "reason=network" in loc:
            fail("token exchange reached fake intervals.icu",
                 "reason=network — the TLS refusal the rider hit is still there")
        elif f"reason=exchange&status={FAKE_ICU_STATUS}" not in loc:
            fail("token exchange reached fake intervals.icu", f"unexpected redirect {loc[:200]}")
        else:
            print(f"e2e OK: token POST went through the interceptor and got HTTP {FAKE_ICU_STATUS} from the fake intervals.icu")

    # The frozen EXE writes %USERPROFILE%\.domestique\logs\domestique.log (plus
    # per-session app_*.log files); scan every log there, oldest first.
    logdir = Path.home() / ".domestique" / "logs"
    files = sorted(logdir.glob("*.log"), key=os.path.getmtime) if logdir.is_dir() else []
    print(f"app logs: {[f.name for f in files]}")
    lines = []
    for f in files:
        lines += [l for l in f.read_text(encoding="utf-8", errors="replace").splitlines() if "icu_oauth" in l]
    print("app log (icu_oauth lines):\n  " + "\n  ".join(lines[-12:]) if lines else "app log: no icu_oauth lines found")
    if not any("EVENT=icu_oauth_start" in l and "tls=os-native" in l for l in lines):
        fail("log", "no 'EVENT=icu_oauth_start ... tls=os-native' line")
    if not any(f"EVENT=icu_oauth_exchange_http status={FAKE_ICU_STATUS}" in l for l in lines):
        fail("log", f"no 'EVENT=icu_oauth_exchange_http status={FAKE_ICU_STATUS}' line")
    if failures:
        print("e2e FAILED:\n  " + "\n  ".join(failures))
        return 1
    print("e2e: all checks passed")
    return 0


def main() -> int:
    # The runner's console is cp1252; never let a log character abort a gate.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if sys.platform != "win32":
        print("tls_intercept_probe_win.py: Windows only (certutil + CryptoAPI + SChannel oracle)")
        return 2
    mode = sys.argv[1] if len(sys.argv) > 1 else "probe"
    return {"probe": probe, "serve-fake-icu": serve_fake_icu, "e2e-oauth": e2e_oauth}[mode]()


if __name__ == "__main__":
    sys.exit(main())
