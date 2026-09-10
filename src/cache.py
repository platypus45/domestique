"""The shared response cache.

Lifted out of app.py, where it sat inside the setup-wizard section while being
read from ten other sections and mutated directly -- past its own API -- from
four of them. That made it an invisible channel between every would-be module,
and it is why the substrate has to move before any feature area can.

`clear_cache()` keeps a registry of extra clearers rather than naming them.
The fatigue-resistance memo cannot live here: it forward-references profile
resolution, which would drag the profile machinery into the substrate and
defeat the point. app.py registers it instead.
"""
import time

from obs import error_codes, _log_error

# ── Cache ─────────────────────────────────────────────────────────────────────

_cache = {}
_cache_ts = {}


def _defensive_copy(value):
    """Return a shallow copy, so a caller cannot mutate what the cache serves.

    `cached()` used to hand every caller the SAME object. One of them mutates
    it -- app.py's readiness endpoint writes severity/source/severity_reasons
    into the dict it was handed -- so those keys were baked into the cache for
    every subsequent reader. More generally it meant clear_cache() did not
    really invalidate: callers went on holding, and mutating, what had been
    the cache's object.

    Shallow, not deep, and deliberately so. The observed failure is a
    top-level key assignment; nothing mutates nested structures. Deep-copying
    would prevent a bug that does not exist, at the cost of copying a 17 MB
    ride archive on every one of the twenty-two `all_rides` reads per request.
    A caller that needs to mutate something nested copies it itself.
    """
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return list(value)
    return value


def cached(key, fn, ttl=300):
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < ttl:
        return _defensive_copy(_cache[key])
    try:
        result = fn()
    except Exception as e:
        # If API call fails (no internet, DNS error), return stale cache.
        if key in _cache:
            return _defensive_copy(_cache[key])
        # v1.6.0 — log under E_CACHE_<key>-or-GENERIC and stick the empty
        # result for only 30s so transient errors don't sit in cache for
        # the full ttl. Trick: backdate _cache_ts to (now - (ttl - 30))
        # so the staleness check ``now - ts < ttl`` flips back to False
        # after 30 wall-clock seconds.
        cache_code = {
            "training": error_codes.Codes.CACHE_TRAINING,
            "sleep": error_codes.Codes.CACHE_SLEEP,
            "wellness": error_codes.Codes.CACHE_WELLNESS,
        }.get(key, error_codes.Codes.CACHE_GENERIC)
        _log_error(cache_code, exc=e, cache_key=key)
        _cache[key] = {}
        if ttl > 30:
            _cache_ts[key] = now - (ttl - 30)
        else:
            _cache_ts[key] = now
        return {}
    _cache[key] = result
    _cache_ts[key] = now
    return _defensive_copy(result)

_extra_clearers: list = []


def register_clearer(fn) -> None:
    """Register a callable to run on clear_cache().

    For caches that cannot live in this module -- anything keyed on state
    the substrate must not know about. Keeps clear_cache() from importing
    the things it clears.
    """
    _extra_clearers.append(fn)


def clear_cache():
    _cache.clear()
    _cache_ts.clear()
    # AC2c: the fatigue-resistance memo is keyed on (ride_id, ftp, window, kj)
    # only — NOT profile — so a profile switch (clear_cache is an on_switch
    # callback) must drop it or profile B could serve A's cached curve.
    for fn in tuple(_extra_clearers):
        try:
            fn()
        except Exception:
            # A clearer that raises must not leave the rest uncleared.
            pass
