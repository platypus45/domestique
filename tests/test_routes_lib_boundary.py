"""routes_lib owns its caches, and nobody may import them by value.

`_load_routes_v2` and `_load_surface_types_db` rebind module globals under a
lock. A `from routes_lib import _ROUTES_CACHE` anywhere binds the list object
that existed at import time: the importer then reads a cache that stops
updating, and -- the case that actually bit -- a test that resets `app.X`
leaves routes_lib reading the real routes.json and asserts against whatever it
happens to hold. Three tests in test_route_picker_api.py failed exactly that
way during the move, which is how we know the hazard is not theoretical.

Same rule as WORKOUT_DIR in test_profile_path_rebinding.py: import the module,
touch the attribute.
"""
import pathlib
import sys
import unittest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
import routes_lib  # noqa: E402

STATE = ("_ROUTES_CACHE", "_ROUTES_INDEX", "_ROUTES_MTIME",
         "_SURFACE_TYPES_CACHE", "_SURFACE_TYPES_MTIME")


class RoutesLibBoundaryTests(unittest.TestCase):
    def test_no_module_binds_the_cache_objects(self):
        """Parsed, not grepped.

        The first version of this scanned single lines for `from X import Y`
        and a review broke it in one try: the parenthesised multi-line form
        walked straight past it -- which is the form app.py itself uses for
        every re-export block. `import routes_lib; X = routes_lib._ROUTES_CACHE`
        got through too. Both are the same hazard, so the check reads the AST
        and looks at every module under src/, subdirectories included.
        """
        import ast
        offenders = []
        for f in sorted(SRC.rglob("*.py")):
            if f.name == "routes_lib.py":
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            rel = f.relative_to(SRC)
            for node in ast.walk(tree):
                # from routes_lib import _ROUTES_CACHE  (any layout)
                if isinstance(node, ast.ImportFrom):
                    for a in node.names:
                        if a.name in STATE:
                            offenders.append(f"{rel}:{node.lineno}: from {node.module} import {a.name}")
            # Only MODULE-SCOPE binds. `d = app.WORKOUT_DIR` inside a
            # function is a fresh read on every call and is the correct
            # pattern -- icu_calendar_push.py:291 and launcher.py:702 both do
            # it. At module scope the same line snapshots the boot value.
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    for v in ast.walk(node.value):
                        if isinstance(v, ast.Attribute) and v.attr in STATE:
                            offenders.append(f"{rel}:{node.lineno}: {ast.unparse(node)[:70]}")
                # X = routes_lib._ROUTES_CACHE  -- binds the same object

        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_no_module_defines_its_own_copy_of_the_state(self):
        """A second `_ROUTES_CACHE = []` anywhere is the same divergence by a
        different route, and the AST sees it at any nesting depth."""
        import ast
        offenders = []
        for f in sorted(SRC.rglob("*.py")):
            if f.name == "routes_lib.py":
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, ast.AnnAssign):
                    targets = [node.target]
                for tgt in targets:
                    if isinstance(tgt, ast.Name) and tgt.id in STATE:
                        offenders.append(f"{f.relative_to(SRC)}:{node.lineno}: {tgt.id} = ...")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_app_does_not_carry_the_cache_names_at_all(self):
        """Not even as a stale copy: a test that patches `app._ROUTES_CACHE`
        should fail loudly with AttributeError rather than pass while patching
        nothing that anyone reads."""
        import app
        for name in STATE:
            self.assertFalse(hasattr(app, name),
                             f"app.{name} exists; monkeypatching it would be a silent no-op")

    def test_the_state_lives_here(self):
        for name in STATE:
            self.assertTrue(hasattr(routes_lib, name), name)

    def test_routes_lib_is_a_leaf(self):
        """It must not import app, fastapi or training_planner, or the module
        is not separable and the next extraction inherits the cycle."""
        import ast
        tree = ast.parse((SRC / "routes_lib.py").read_text(encoding="utf-8"))
        roots = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                roots |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                roots.add(n.module.split(".")[0])
        self.assertEqual(roots & {"app", "fastapi", "training_planner", "starlette"}, set(),
                         f"routes_lib imports {sorted(roots)}")

    def test_app_re_exports_the_functions_as_the_same_objects(self):
        """The 19 helpers ARE re-exported -- functions are safe to bind, and
        60 test files reach them through `app.X`."""
        import app
        for name in ("_load_routes_v2", "_canonical_surface", "_load_surface_types_db",
                     "_is_climb_route", "_is_flat_route", "_route_summary",
                     "_apply_route_filters", "_score_route_for_suggest",
                     "_gradient_to_power_factor", "_build_climb_zwo",
                     "_load_route_detail", "_route_surface_segments"):
            self.assertIs(getattr(app, name), getattr(routes_lib, name), name)


if __name__ == "__main__":
    unittest.main()
