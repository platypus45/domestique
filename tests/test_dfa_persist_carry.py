"""v2.2.9 — re-persisting an ICU activity must NOT wipe locally-computed DFA.

Regression: ICU payloads carry no DFA α1 / HRVT thresholds — those are computed
locally from the FIT by _augment_icu_record_with_dfa. persist_icu_activity
overwrote the file with the DFA-less ICU payload on every re-sync (status
"updated"), wiping dfa_alpha1_avg / dfa_hrvt1 / dfa_hrvt2. With the per-sync
augment budget only recomputing a few, the DFA threshold aggregate collapsed to
"no thresholds detected". persist_icu_activity now carries the DFA fields
forward (like it already does for `prs`).
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import ride_storage as rs


def _icu_activity(ext_id="i999", name="Test Ride"):
    # Minimal shape _normalize_icu_activity accepts. No DFA fields (ICU never
    # sends them).
    return {"id": ext_id, "name": name, "type": "Ride",
            "start_date_local": "2026-06-21T10:00:00", "moving_time": 3600}


class TestDfaCarryForward(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self._dir = Path(self._td.name)
        self._patch = patch.object(rs, "_icu_rides_dir", return_value=self._dir)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._td.cleanup()

    def test_resync_preserves_local_dfa(self):
        act = _icu_activity()
        p = rs.persist_icu_activity(act)
        self.assertIsNotNone(p)
        # Simulate _augment_icu_record_with_dfa writing computed DFA back.
        rec = json.loads(p.read_text())
        rec.update({
            "dfa_alpha1_avg": 0.78, "dfa_alpha1_status": "computed",
            "dfa_hrvt1": {"hr": 142, "power": 210},
            "dfa_hrvt2": {"hr": 168, "power": 290},
            "dfa_zone_minutes": {"z1": 30, "z2": 20, "z3": 10},
            "rr_intervals_count": 5400, "dfa_algo_version": 3,
        })
        p.write_text(json.dumps(rec))
        # Re-sync: same activity, DFA-less ICU payload.
        rs.persist_icu_activity(_icu_activity())
        after = json.loads(p.read_text())
        self.assertEqual(after.get("dfa_alpha1_avg"), 0.78, "dfa_alpha1_avg was wiped on re-persist")
        self.assertEqual(after.get("dfa_alpha1_status"), "computed")
        self.assertEqual(after.get("dfa_hrvt1"), {"hr": 142, "power": 210}, "hrvt1 wiped")
        self.assertEqual(after.get("dfa_hrvt2"), {"hr": 168, "power": 290}, "hrvt2 wiped")
        self.assertEqual(after.get("rr_intervals_count"), 5400)

    def test_fresh_persist_has_no_dfa_keys(self):
        # A brand-new record (no prior file) carries no DFA keys — augment fills
        # them later. Just assert persist succeeds + doesn't invent values.
        p = rs.persist_icu_activity(_icu_activity(ext_id="i1000"))
        rec = json.loads(p.read_text())
        self.assertIsNone(rec.get("dfa_alpha1_avg"))


if __name__ == "__main__":
    unittest.main()
