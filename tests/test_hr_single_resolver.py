"""HR numbers must come from one place.

`_prescription_hr_rows` is the single resolver: the converter, both FIT
builders, the hr_axis, `/api/settings` and the session chips all route through
it, which is the only reason the number on a chip matches the number in the
exported .fit. It sat inside the FIT WORKOUT EXPORT section, where six callers
outside that section had to reach into an export detail to get it.

These tests pin the property the move is for: one definition, and app.py's name
is that same object rather than a copy.
"""
import pathlib
import re
import sys
import unittest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
import hr  # noqa: E402

NAMES = ("_fit_hr_mode", "_prescription_hr_rows", "_hr_bias", "_fit_hr_params")


class SingleResolverTests(unittest.TestCase):
    def test_each_name_is_defined_exactly_once_in_src(self):
        """A second `def` anywhere in src/ is the drift this move prevents:
        two resolvers disagreeing by a few bpm is invisible until an athlete
        rides the wrong zone."""
        offenders = []
        for name in NAMES:
            sites = [
                f"{f.name}:{i}"
                for f in sorted(SRC.glob("*.py"))
                for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
                if re.match(rf"def {name}\(", line)
            ]
            if len(sites) != 1 or not sites[0].startswith("hr.py:"):
                offenders.append(f"{name}: {sites}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_app_re_exports_the_same_objects(self):
        """Not a copy: `app._hr_bias is hr._hr_bias`. The 60 test files that
        reach `app.X` must see the object hr.py holds, or monkeypatching one
        leaves the other live."""
        import app
        for name in NAMES:
            self.assertIs(getattr(app, name), getattr(hr, name), name)

    def test_custom_rows_pass_through_only_when_a_dict(self):
        class PM:
            def __init__(self, v):
                self._athlete = {"hr_prescription_rows_custom": v}

        rows = {"z1_high": 120, "z2": [121, 140], "z3": [141, 158], "z4": [159, 172]}
        self.assertEqual(hr._prescription_hr_rows(PM(rows)), rows)
        # Anything not a dict means "Coggan defaults", never a half-applied
        # override -- a list or a string here used to reach the FIT builder.
        for junk in (None, [], "z2", 0):
            self.assertIsNone(hr._prescription_hr_rows(PM(junk)), junk)

    def test_absent_key_means_defaults(self):
        class PM:
            _athlete = {}
        self.assertIsNone(hr._prescription_hr_rows(PM()))

    def test_hr_bias_follows_fit_hr_mode(self):
        """One chokepoint: every rematch/redraw path agrees because _hr_bias
        is _fit_hr_mode, not a parallel reading of target_mode."""
        real = hr._fit_hr_mode
        try:
            hr._fit_hr_mode = lambda: True
            self.assertTrue(hr._hr_bias())
            hr._fit_hr_mode = lambda: False
            self.assertFalse(hr._hr_bias())
        finally:
            hr._fit_hr_mode = real

    def test_hr_mode_is_false_when_profile_manager_is_unavailable(self):
        """Degrades to power rather than raising. The FIT builders call this
        on every export; an import error there would fail the download, not
        fall back."""
        import builtins
        real = builtins.__import__

        def blow_up(name, *a, **k):
            if name == "profile_manager":
                raise ImportError("no profile_manager")
            return real(name, *a, **k)

        builtins.__import__ = blow_up
        try:
            self.assertFalse(hr._fit_hr_mode())
        finally:
            builtins.__import__ = real

    def test_hr_module_stands_alone(self):
        """It must not import fastapi, app or training_planner at module
        scope, or it is not a leaf and the workout-library extraction still
        drags the whole file in."""
        src = (SRC / "hr.py").read_text(encoding="utf-8")
        top_level = [l for l in src.splitlines() if l.startswith(("import ", "from "))]
        self.assertEqual(top_level, [], "\n".join(top_level))


if __name__ == "__main__":
    unittest.main()
