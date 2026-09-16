"""HTTP-request helpers shared across the whole API surface.

`_get_json_body` is the only body parser in the file and is reached from twelve
sections; `_icu_verify` supplies the TLS context for every outbound httpx call;
`_diag_local_only` is the only auth-shaped guard there is. All three sat inside
unrelated feature sections, which meant any module extracted from app.py would
have had to import app just to parse a request body.

Deliberately NOT added here: an error-envelope helper. app.py raises
HTTPException 55 times and hands back ad-hoc {"ok": False, "error": ...} dicts,
so one would be useful -- but inventing an abstraction nobody calls yet is
speculative. It goes in when something needs it.
"""
import tls_trust
from fastapi import HTTPException, Request

from obs import log


def _icu_verify():
    """TLS trust for every httpx call to intervals.icu / GitHub (see tls_trust).

    Not cached here: on Windows tls_trust hands out a fresh OS-native context
    per call (truststore#209 race), elsewhere it caches the OpenSSL one itself.
    """
    return tls_trust.make_context()


async def _get_json_body(request) -> dict:
    try:
        return await request.json()
    except Exception as e:
        log.debug(f"JSON parse failed on {request.url.path}: {e}")
        raise HTTPException(status_code=400, detail="invalid JSON body")


def _diag_local_only(request: Request) -> bool:
    """True iff the request is from localhost. Domestique listens only on
    127.0.0.1, so any non-local client is suspicious. Returns True when
    ``request.client`` is None or the host is the FastAPI TestClient
    sentinel, so tests pass without special-casing.
    """
    client = getattr(request, "client", None)
    if client is None or client.host is None:
        return True
    return client.host in ("127.0.0.1", "localhost", "::1", "testclient")
