"""A stranger on :8080 must not be mistaken for Domestique.

Reported from Pop!_OS 24.04 / COSMIC / Wayland: "I get a webpage that says no
cameras and lost connection. No prompt to connect to intervals.icu. I can't
find that file."

All four symptoms are one bug. ``is_already_running()`` probed the URL and
returned True if ANYTHING answered, with no identity check. The tester had a
camera web UI on 8080, so Domestique concluded it was already running and
pointed its window at that server: a page about cameras, a "lost connection"
from its socket, no intervals.icu onboarding because Domestique never started
— and no crash file, because from the launcher's point of view nothing had
gone wrong. The one instruction we could give ("send the crash file") named a
file that could not exist.

Pinned here:
  1. a foreign server on the port is NOT us;
  2. a real Domestique IS us, including one predating the identity marker,
     so upgrading does not make a new launcher refuse an old instance;
  3. losing the port is fatal AND visible, on all three channels.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import launcher


class _Resp:
    """Minimal stand-in for urlopen's context-managed response."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _serving(monkeypatch, payload):
    """Pretend :8080 answers /api/version with ``payload``."""
    import urllib.request
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(body))


# The camera app the tester actually had on 8080 — a real page, not JSON.
_CAMERA_APP = b"<!doctype html><title>Cameras</title><h1>No cameras found</h1>"


@pytest.mark.parametrize("payload,label", [
    (_CAMERA_APP, "a camera web UI serving HTML"),
    ({"status": "ok"}, "a health endpoint with no identity"),
    ({"version": "1.2.3"}, "a version endpoint that is not ours"),
    (b"not json at all", "a server returning garbage"),
])
def test_a_stranger_on_our_port_is_not_domestique(payload, label, monkeypatch):
    _serving(monkeypatch, payload)
    assert launcher.is_domestique_at("http://localhost:8080") is False, (
        f"{label} was accepted as a running Domestique — the window would "
        f"have shown that app instead of ours")


def test_a_real_domestique_is_recognised(monkeypatch):
    _serving(monkeypatch, {"app": "domestique", "version": "4.0.0-alpha",
                           "python": "3.12.0", "frozen": True,
                           "data_dir": "/home/x/.domestique"})
    assert launcher.is_domestique_at("http://localhost:8080") is True


def test_an_instance_predating_the_marker_is_still_recognised(monkeypatch):
    """Upgrade path: a running 3.8.x has no "app" key but is still us."""
    _serving(monkeypatch, {"version": "3.8.1", "python": "3.12.0",
                           "frozen": True, "data_dir": "/home/x/.domestique"})
    assert launcher.is_domestique_at("http://localhost:8080") is True, (
        "a new launcher refused an older running instance — it would "
        "declare the port squatted and refuse to start")


def test_unreachable_port_is_not_domestique(monkeypatch):
    import urllib.request

    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert launcher.is_domestique_at("http://localhost:8080") is False


def test_version_endpoint_carries_the_marker():
    """The launcher's probe is only as good as what the app answers with."""
    import app as app_mod
    assert app_mod.api_version().get("app") == "domestique", (
        "the identity marker the launcher probes for is gone from "
        "/api/version — single-instance detection silently degrades to "
        "'did anything answer?'")


def test_losing_the_port_is_fatal_and_visible(monkeypatch, capsys):
    """print() + sys.exit(2) is an invisible death from a desktop icon."""
    import socket

    class _Sock:
        def bind(self, *a):
            raise OSError("Address already in use")

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", lambda *a, **k: _Sock())

    with pytest.raises(SystemExit) as exc:
        launcher._ensure_port_free_or_die()
    assert exc.value.code != 0

    crash = Path.home() / ".domestique" / "startup_crash.txt"
    assert crash.exists(), "no crash report — the failure is invisible again"
    text = crash.read_text(encoding="utf-8")
    assert str(launcher.PORT) in text
    # Every candidate is named, so the user knows the app did try to recover
    # before giving up.
    for port in launcher.PORT_CANDIDATES:
        assert str(port) in text
    # The report has to tell the user how to find the squatter, and how to
    # force a port; "port in use" alone is what left the tester with nothing
    # to act on.
    assert "ss -ltnp" in text or "lsof" in text
    assert "DOMESTIQUE_PORT" in text
    assert "free port" in capsys.readouterr().err.lower()
