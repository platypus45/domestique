"""Workout and GPX paths must be read late, not captured at import.

`_apply_profile_paths()` rebinds the module globals WORKOUT_DIR and GPX_DIR on
every profile switch, so a profile without its own override never inherits the
previous profile's directories. Anything holding a value captured earlier keeps
serving the old profile's workouts, and does it silently -- no exception, no
log line, just the wrong athlete's library.

That is fine while everything lives in one module and reads the global at call
time. It stops being fine the moment a second module does
`from app import WORKOUT_DIR`, which binds the value, not the name.

These tests pin the hazard so the decomposition cannot reintroduce it.
"""
import inspect
import pathlib
import unittest

import app


class ProfilePathRebindingTests(unittest.TestCase):
    def setUp(self):
        self._workout = app.WORKOUT_DIR
        self._gpx = app.GPX_DIR

    def tearDown(self):
        app.WORKOUT_DIR = self._workout
        app.GPX_DIR = self._gpx

    def test_a_captured_value_goes_stale(self):
        """The hazard itself: binding the value survives the rebind."""
        captured = app.WORKOUT_DIR              # what `from app import ...` does
        app.WORKOUT_DIR = pathlib.Path("/tmp/profile-b/workouts")
        self.assertNotEqual(captured, app.WORKOUT_DIR)

    def test_the_accessor_tracks_the_rebind(self):
        app.WORKOUT_DIR = pathlib.Path("/tmp/profile-b/workouts")
        self.assertEqual(app.active_workout_dir(), pathlib.Path("/tmp/profile-b/workouts"))

    def test_the_gpx_accessor_tracks_too(self):
        app.GPX_DIR = pathlib.Path("/tmp/profile-b/gpx")
        self.assertEqual(app.active_gpx_dir(), pathlib.Path("/tmp/profile-b/gpx"))

    def test_accessors_agree_with_the_globals_when_nothing_has_moved(self):
        self.assertEqual(app.active_workout_dir(), app.WORKOUT_DIR)
        self.assertEqual(app.active_gpx_dir(), app.GPX_DIR)

    def test_apply_profile_paths_still_rebinds(self):
        """Guards the mechanism the accessors depend on: if this stops
        rebinding, the accessors are correct and useless.

        Read rather than run. Calling it for real builds the ProfileManager
        singleton inside the shared test sandbox, so a later test expecting a
        fresh install stops seeing one -- that cost 22 unrelated failures when
        this test drove the function directly.
        """
        src = inspect.getsource(app._apply_profile_paths)
        self.assertIn("global WORKOUT_DIR, GPX_DIR", src)
        self.assertIn("WORKOUT_DIR = wp", src)
        self.assertIn("GPX_DIR = gp", src)

    def test_no_module_binds_the_value_instead_of_the_name(self):
        """The rule, enforced rather than documented.

        `from <anything> import WORKOUT_DIR` binds the object. After a profile
        switch that name still points at the old directory, with nothing to
        indicate it. Import the module and read the attribute, or call the
        accessor.

        Parsed rather than grepped: the line-scanning version of this check
        missed the parenthesised multi-line form, which is exactly the form
        app.py uses for every import block, and missed
        `import app; D = app.WORKOUT_DIR`, which binds the same object by a
        different spelling. rglob so src/scripts/ is covered too.
        """
        import ast
        src = pathlib.Path(__file__).resolve().parent.parent / "src"
        names = ("WORKOUT_DIR", "GPX_DIR")
        offenders = []
        for f in sorted(src.rglob("*.py")):
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            rel = f.relative_to(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for a in node.names:
                        if a.name in names:
                            offenders.append(f"{rel}:{node.lineno}: from {node.module} import {a.name}")
            # Only MODULE-SCOPE binds. `d = app.WORKOUT_DIR` inside a
            # function is a fresh read on every call and is the correct
            # pattern -- icu_calendar_push.py:291 and launcher.py:702 both do
            # it. At module scope the same line snapshots the boot value.
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    for v in ast.walk(node.value):
                        if isinstance(v, ast.Attribute) and v.attr in names:
                            offenders.append(f"{rel}:{node.lineno}: {ast.unparse(node)[:70]}")

        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
