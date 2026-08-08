"""Port selection: zero-config, stable across restarts, overridable.

The rider is never asked to pick a port — being prompted for one on first run
would be a worse experience than the collision it avoids. So the flow has to
hold up unattended:

  * the default must not be a port anything else wants (22400);
  * it must not sit in any OS ephemeral pool, or it fails to bind
    intermittently rather than never (see the band assertions below);
  * a busy port falls back instead of refusing to launch;
  * an ALREADY-RUNNING Domestique is not a collision — falling back there
    would start a second server instead of focusing the open window;
  * whatever bound last time is preferred next time, so bookmarks survive;
  * DOMESTIQUE_PORT overrides everything, with no second-guessing.
"""
from __future__ import annotations

import launcher


# Union of the default ephemeral pools: Linux ip_local_port_range 32768-60999,
# macOS ip.portrange.first and the Windows dynamic range both 49152-65535.
# A listener inside these can be transiently stolen by an outbound connection.
_EPHEMERAL_FLOOR = 32768
_UNPRIVILEGED_FLOOR = 1024


def test_every_candidate_is_bindable_without_privileges():
    for port in launcher.PORT_CANDIDATES:
        assert port >= _UNPRIVILEGED_FLOOR, (
            f"{port} needs root to bind on Linux/macOS")


def test_no_candidate_sits_in_an_ephemeral_range():
    """The failure this prevents is intermittent, so it survives manual testing.

    A port above 32768 is inside Linux's outbound pool: it binds fine most of
    the time and fails whenever a connection happens to hold it — after a
    reboot, behind a VPN, or under heavy browsing.
    """
    for port in launcher.PORT_CANDIDATES:
        assert port < _EPHEMERAL_FLOOR, (
            f"{port} is inside an OS ephemeral range; binding it will fail "
            f"intermittently rather than never")


def test_default_is_the_researched_port():
    assert launcher.DEFAULT_PORT == 22400
    assert launcher.PORT_CANDIDATES[0] == 22400


def test_explicit_override_wins_outright(monkeypatch):
    """A deliberate choice must not be silently overruled by a fallback."""
    monkeypatch.setenv("DOMESTIQUE_PORT", "23500")
    monkeypatch.setattr(launcher, "_port_is_available", lambda p: False)
    assert launcher._resolve_port() == 23500


def test_a_garbled_override_falls_back_instead_of_crashing(monkeypatch):
    monkeypatch.setenv("DOMESTIQUE_PORT", "not-a-port")
    monkeypatch.setattr(launcher, "_port_is_available", lambda p: True)
    assert launcher._resolve_port() == launcher.DEFAULT_PORT


def test_a_busy_default_falls_through_to_the_next_candidate(monkeypatch):
    monkeypatch.delenv("DOMESTIQUE_PORT", raising=False)
    monkeypatch.setattr(launcher, "_port_memo", lambda: _Missing())
    taken = {launcher.PORT_CANDIDATES[0]}
    monkeypatch.setattr(launcher, "_port_is_available", lambda p: p not in taken)
    assert launcher._resolve_port() == launcher.PORT_CANDIDATES[1]


def test_the_remembered_port_is_preferred(monkeypatch, tmp_path):
    """A stable URL is what makes a bookmark or desktop shortcut keep working."""
    monkeypatch.delenv("DOMESTIQUE_PORT", raising=False)
    memo = tmp_path / "port.txt"
    memo.write_text("26214\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "_port_memo", lambda: memo)
    monkeypatch.setattr(launcher, "_port_is_available", lambda p: True)
    assert launcher._resolve_port() == 26214, (
        "the port bound last time was not tried first — the app would move "
        "back to the default and break saved links")


def test_exhausting_every_candidate_still_reports_properly(monkeypatch):
    """Return the default so the caller reaches the visible failure path.

    Dying here instead would lose the diagnostics — which is precisely how the
    original bug reached a user with nothing to act on.
    """
    monkeypatch.delenv("DOMESTIQUE_PORT", raising=False)
    monkeypatch.setattr(launcher, "_port_memo", lambda: _Missing())
    monkeypatch.setattr(launcher, "_port_is_available", lambda p: False)
    assert launcher._resolve_port() == launcher.DEFAULT_PORT


def test_a_running_domestique_does_not_count_as_a_collision(monkeypatch):
    """Otherwise a second launch starts a SECOND server on the next port
    instead of focusing the window that is already open."""
    import socket

    class _Bound:
        def bind(self, *a):
            raise OSError("Address already in use")

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", lambda *a, **k: _Bound())
    monkeypatch.setattr(launcher, "is_domestique_at", lambda url: True)
    assert launcher._port_is_available(22400) is True


def test_a_stranger_on_the_port_does_count_as_a_collision(monkeypatch):
    import socket

    class _Bound:
        def bind(self, *a):
            raise OSError("Address already in use")

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", lambda *a, **k: _Bound())
    monkeypatch.setattr(launcher, "is_domestique_at", lambda url: False)
    assert launcher._port_is_available(22400) is False


def test_the_url_uses_the_ip_literal_not_localhost():
    """RFC 8252 §8.3: localhost can resolve to a non-loopback interface and
    breaks on a mangled hosts file or a client firewall."""
    assert launcher.URL.startswith("http://127.0.0.1:")
    assert "localhost" not in launcher.URL


def test_the_oauth_callback_follows_the_bound_port(monkeypatch):
    """The callback must land on the port we actually bound, or the
    intervals.icu round-trip dead-ends on a closed socket."""
    import importlib
    monkeypatch.setenv("DOMESTIQUE_PORT", "26214")
    import config
    importlib.reload(config)
    try:
        assert config.ICU_OAUTH_REDIRECT_URI == (
            "http://127.0.0.1:26214/oauth/icu/callback")
        # intervals.icu rejects [::1] and matches the host case-sensitively.
        assert "[::1]" not in config.ICU_OAUTH_REDIRECT_URI
        assert config.ICU_OAUTH_REDIRECT_URI.startswith("http://127.0.0.1:")
    finally:
        monkeypatch.delenv("DOMESTIQUE_PORT", raising=False)
        importlib.reload(config)


def test_remember_port_survives_an_unwritable_home(monkeypatch, tmp_path):
    """A URL we cannot remember is not worth failing a launch over."""
    def _boom():
        raise OSError("read-only filesystem")

    monkeypatch.setattr(launcher, "_port_memo", _boom)
    launcher._remember_port(22400)   # must not raise


class _Missing:
    """A port memo that does not exist yet (first run)."""

    def read_text(self, **kw):
        raise OSError("no such file")
