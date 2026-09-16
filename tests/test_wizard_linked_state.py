"""The intervals.icu step of both wizards reads as done once an account is
linked (IP_profile_setup_linked_state, 2026-09-16).

Before: a 13 px line "Linked as <name>", the big accent button relabelled
"Re-link a different account", and a 12 px grey "Go to dashboard" as the only
way forward. Now: three states, one primary action each.

  not linked -> primary "Sign in to intervals.icu", small "Skip for now"
  linked     -> a linked-account card (check, "Connected to intervals.icu",
                the athlete's name, the id), primary "Finish setup"
                (profile wizard) / "Continue" (first-run wizard), the re-link
                demoted to a small secondary button "Use a different account"
  error      -> the red reason, primary "Try again", skip still there

The JS is executed from the real templates under node against a minimal DOM
and a fake fetch, so the assertions are on behaviour, not on source text.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PROFILE_SETUP = REPO / "src" / "templates" / "profile_setup.html"
SETUP = REPO / "src" / "templates" / "setup.html"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _extract_js_function(src: str, name: str) -> str:
    start = src.find(f"async function {name}")
    if start < 0:
        start = src.index(f"function {name}")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


FAKE_DOM = r"""
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ''; this.hidden = false;
                     this.style = {}; this._text = ''; this.focused = false; this.attrs = {}; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(v) { throw new Error('innerHTML used: ' + v); }   // A5: never HTML
  appendChild(c) { this.children.push(c); return c; }
  focus() { this.focused = true; }
  setAttribute(k, v) { this.attrs[k] = v; }
}
const els = {};
const document = {
  getElementById: id => els[id] || null,
  createElement: tag => new El(tag),
};
function mk(id, cls, text) { const e = new El('x'); e.className = cls || ''; e._text = text || ''; els[id] = e; return e; }
let fetchPayload = null;
async function fetch(url) { return { json: async () => fetchPayload }; }
function assert(cond, msg) { if (!cond) { console.error('ASSERT: ' + msg); process.exit(1); } }
"""


def _run_node(harness: str) -> None:
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, f"stderr:\n{res.stderr}\nstdout:\n{res.stdout}"


# ── static markup ────────────────────────────────────────────────────────────

def test_profile_wizard_markup_has_the_three_controls():
    s = PROFILE_SETUP.read_text()
    assert 'id="step3-finish" hidden>Finish setup</a>' in s
    assert 'class="btn btn-primary" id="step3-finish"' in s
    assert 'id="connect-icu-btn" onclick="connectIcu()">Sign in to intervals.icu</button>' in s
    assert 'class="skip-link" id="step3-done"' in s and 'Continue without intervals.icu' in s
    assert 'href="https://intervals.icu/signup" target="_blank" rel="noopener noreferrer" class="btn btn-secondary" id="step3-signup">Create a free account</a>' in s
    assert 'id="icu-choice"' in s and s.count('class="choice-card') == 2
    assert 'id="link-status" role="status" aria-live="polite"' in s
    assert "Re-link a different account" not in s
    assert "Go to dashboard" not in s and "Skip for now" not in s


def test_first_run_wizard_markup_keeps_continue_as_the_primary():
    s = SETUP.read_text()
    assert 'id="setup-link-status" role="status" aria-live="polite"' in s
    assert 'class="btn btn-secondary" onclick="nextStep(2)" id="step1-next">Continue without intervals.icu</button>' in s
    assert 'id="setup-signup">Create a free account</a>' in s and 'id="setup-icu-choice"' in s
    assert "Re-link a different account" not in s


def test_oauth_binding_untouched():
    """A4: connectIcu still stashes the wizard position and redirects to the
    OAuth start with the return path; the fix is presentation only."""
    fn = _extract_js_function(PROFILE_SETUP.read_text(), "connectIcu")
    assert "sessionStorage.setItem('ps_wizard'" in fn
    assert "/oauth/icu/start?return_to=" in fn and "/profile-setup" in fn
    fn2 = _extract_js_function(SETUP.read_text(), "connectIcu")
    assert "/oauth/icu/start?return_to=" in fn2 and "/setup" in fn2


# ── behaviour, from the real templates ───────────────────────────────────────

@needs_node
def test_profile_wizard_three_states():
    src = PROFILE_SETUP.read_text()
    fns = "\n".join(_extract_js_function(src, n) for n in ("_renderLinkedCard", "_setLinkState", "refreshLinkStatus"))
    harness = FAKE_DOM + fns + r"""
const status = mk('link-status'); const btn = mk('connect-icu-btn', 'btn btn-primary', 'Sign in to intervals.icu');
const fin = mk('step3-finish', 'btn btn-primary', 'Finish setup'); fin.hidden = true; const skip = mk('step3-done', 'skip-link', 'Continue without intervals.icu');
const signup = mk('step3-signup', 'btn btn-secondary', 'Create a free account'); const choice = mk('icu-choice');
(async () => {
  // linked: the round-trip came back, the connection is oauth
  fetchPayload = { method: 'oauth', name: 'Loes <b>x</b>', athlete_id: 138904, connected: true };
  await refreshLinkStatus('ok');
  const text = status.textContent;
  assert(text.includes('Connected to intervals.icu'), 'card title missing: ' + text);
  assert(text.includes('Loes <b>x</b>'), 'name shown as text, not markup: ' + text);
  assert(text.includes('athlete 138904'), 'athlete id shown');
  assert(text.includes('when you finish'), 'what happens next');
  assert(fin.hidden === false && fin.focused === true, 'Finish setup visible and focused');
  assert(btn.textContent === 'Use a different account' && btn.className.includes('btn-sm') && btn.className.includes('btn-secondary'), 're-link demoted: ' + btn.className + ' / ' + btn.textContent);
  assert(!btn.className.includes('btn-primary'), 're-link no longer primary');
  assert(skip.hidden === true, 'skip link hidden once linked');
  assert(choice.hidden === true && signup.hidden === true, 'comparison and create-account leave once linked');
  // error: back from a failed round-trip
  await refreshLinkStatus('error');
  assert(status.textContent.includes('please try again'), 'error text');
  assert(fin.hidden === true, 'no Finish on error');
  assert(btn.className === 'btn btn-primary' && btn.textContent === 'Try again', 'primary Try again: ' + btn.className + ' / ' + btn.textContent);
  assert(skip.hidden === false && choice.hidden === false && signup.hidden === false, 'choice, create-account and continue-without back on error');
  // not linked: a plain load with no oauth connection
  fetchPayload = { method: null, connected: false };
  await refreshLinkStatus('');
  assert(status.textContent === '', 'status empty when not linked');
  assert(fin.hidden === true && btn.textContent === 'Sign in to intervals.icu' && btn.className === 'btn btn-primary', 'sign-in primary when not linked');
  assert(skip.hidden === false, 'skip visible when not linked');
  // A6: a plain reload with an existing oauth connection renders linked too
  fetchPayload = { method: 'oauth', name: '', athlete_id: 7, connected: true };
  await refreshLinkStatus('');
  assert(status.textContent.includes('Your account') && fin.hidden === false, 'linked state from a plain load, name fallback');
  console.log('ok');
})().catch(e => { console.error(e); process.exit(1); });
"""
    _run_node(harness)


@needs_node
def test_first_run_wizard_demotes_the_relink_and_keeps_the_gated_blocks():
    src = SETUP.read_text()
    fns = "\n".join(_extract_js_function(src, n) for n in ("_renderLinkedCard", "_setLinkState", "refreshLinkStatus"))
    harness = FAKE_DOM + fns + r"""
const status = mk('setup-link-status'); const btn = mk('setup-connect-btn', 'btn', 'Sign in to intervals.icu');
const ar = mk('autofill-row'); ar.style.display = 'none'; const gb = mk('garmin-block'); gb.style.display = 'none';
const next = mk('step1-next', 'btn btn-secondary', 'Continue without intervals.icu'); const signup = mk('setup-signup', 'btn btn-secondary', 'Create a free account'); const choice = mk('setup-icu-choice');
(async () => {
  fetchPayload = { method: 'oauth', name: 'Doug', athlete_id: 138904, connected: true };
  await refreshLinkStatus('ok', '');
  assert(status.textContent.includes('Connected to intervals.icu') && status.textContent.includes('Doug'), 'card');
  assert(status.textContent.includes('setup is complete'), 'first-run wording says the setup continues');
  assert(btn.textContent === 'Use a different account' && btn.className.includes('btn-sm'), 'demoted: ' + btn.className);
  assert(ar.style.display === 'block' && gb.style.display === 'block', 'AC5a gated blocks still revealed');
  assert(next.textContent === 'Continue' && next.className === 'btn btn-primary' && choice.hidden === true && signup.hidden === true, 'linked: Continue is the primary, comparison gone');
  await refreshLinkStatus('error', 'no_athlete_id');
  assert(status.textContent.includes("didn't return an athlete id"), 'AC3d message kept');
  assert(btn.className === 'btn btn-primary' && btn.textContent === 'Try again', 'error: ' + btn.className + ' / ' + btn.textContent);
  assert(next.textContent === 'Continue without intervals.icu' && next.className === 'btn btn-secondary' && choice.hidden === false, 'error: the choice is back, continue-without is secondary');
  fetchPayload = { method: null, connected: false };
  await refreshLinkStatus('', '');
  assert(btn.className === 'btn btn-primary' && btn.textContent === 'Sign in to intervals.icu', 'restored');
  console.log('ok');
})().catch(e => { console.error(e); process.exit(1); });
"""
    _run_node(harness)


def test_templates_still_render():
    """Jinja syntax intact: both wizards serve 200 through the app."""
    import sys
    sys.path.insert(0, str(REPO / "src"))
    import app as app_module
    from fastapi.testclient import TestClient
    client = TestClient(app_module.app)
    for path in ("/profile-setup", "/setup"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (200, 302, 303, 307), (path, r.status_code)
        if r.status_code == 200:
            assert "linked-card" in r.text
