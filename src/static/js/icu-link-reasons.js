// Why an intervals.icu sign-in failed, in words a rider can act on.
// The OAuth callback (app.py, api_oauth_icu_callback) bounces back with
// ?icu=error&reason=<code>[&status=<http>]. The dashboard and both setup
// wizards read this one table: the wizards used to drop the reason and show
// "please try again" for every failure, so a rider's report said nothing
// about which step broke.
var ICU_LINK_REASON_MSGS = {
  no_athlete_id: "intervals.icu didn't return an athlete id for that account. Nothing was linked — try connecting again.",
  profile_gone: 'the profile that started the sign-in no longer exists.',
  state: 'the sign-in link expired — start again from this page.',
  exchange: 'intervals.icu rejected the sign-in code — try again (a code is single-use and expires within minutes).',
  network: "the app could not reach intervals.icu after you signed in. This is usually antivirus \"HTTPS scanning\", a VPN or a work proxy that inspects web traffic; add intervals.icu to its exclusions or try another network, then try again.",
  token: 'intervals.icu returned an unexpected response — try again.',
  purge: "signed in, but clearing the previous account's data failed — see the log, then try again.",
  save: 'signed in, but the token could not be saved to your profile folder (permissions or disk) — fix the folder, then connect again.',
  denied: 'access was declined on intervals.icu.',
  busy: 'a sync was in progress — try again in a moment.'
};

function icuLinkFailureText(reason, status) {
  return (ICU_LINK_REASON_MSGS[reason] || 'try again') + (status ? ' (HTTP ' + status + ')' : '');
}
