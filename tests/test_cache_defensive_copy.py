"""The response cache must not hand out the object it is holding.

`cached()` used to return the stored object itself, so any caller could mutate
what every later reader saw, and `clear_cache()` did not really invalidate --
callers went on holding, and mutating, what had been the cache's object. There
was a live instance: app.py's readiness endpoint writes severity, source and
severity_reasons into the dict it is handed.
"""
import sys
import pathlib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import cache  # noqa: E402


class DefensiveCopyTests(unittest.TestCase):
    def setUp(self):
        cache.clear_cache()

    def test_caller_mutation_does_not_reach_the_next_reader(self):
        cache.cached("k", lambda: {"a": 1})
        first = cache.cached("k", lambda: {"a": 1})
        first["injected"] = True
        second = cache.cached("k", lambda: {"a": 1})
        self.assertNotIn("injected", second)

    def test_caller_mutation_does_not_reach_the_stored_value(self):
        cache.cached("k", lambda: {"a": 1})
        cache.cached("k", lambda: {"a": 1})["injected"] = True
        self.assertNotIn("injected", cache._cache["k"])

    def test_two_callers_get_distinct_objects(self):
        cache.cached("k", lambda: {"a": 1})
        self.assertIsNot(cache.cached("k", lambda: {"a": 1}),
                         cache.cached("k", lambda: {"a": 1}))

    def test_lists_are_copied_too(self):
        cache.cached("rows", lambda: [1, 2, 3])
        cache.cached("rows", lambda: [1, 2, 3]).append(4)
        self.assertEqual(cache._cache["rows"], [1, 2, 3])

    def test_the_first_call_is_copied_as_well(self):
        # The miss path returns the freshly computed value; it must be copied
        # too, or the very first caller still holds the cache's object.
        built = {"a": 1}
        got = cache.cached("k", lambda: built)
        self.assertIsNot(got, cache._cache["k"])

    def test_scalars_pass_through_unwrapped(self):
        self.assertEqual(cache.cached("n", lambda: 7), 7)

    def test_the_copy_is_shallow_and_that_is_documented(self):
        # Stated so the limit is a decision on record rather than a surprise:
        # nested structures are shared, because deep-copying a 17 MB ride
        # archive on every read would cost far more than the bug it prevents.
        cache.cached("nested", lambda: {"inner": {"v": 1}})
        cache.cached("nested", lambda: {"inner": {"v": 1}})["inner"]["v"] = 99
        self.assertEqual(cache._cache["nested"]["inner"]["v"], 99)

    def test_registered_clearers_run_on_clear(self):
        calls = []
        cache.register_clearer(lambda: calls.append(1))
        try:
            cache.clear_cache()
            self.assertEqual(calls, [1])
        finally:
            cache._extra_clearers.pop()

    def test_a_raising_clearer_does_not_block_the_others(self):
        calls = []

        def boom():
            raise RuntimeError("clearer failed")

        cache.register_clearer(boom)
        cache.register_clearer(lambda: calls.append(1))
        try:
            cache.clear_cache()
            self.assertEqual(calls, [1])
        finally:
            cache._extra_clearers.pop()
            cache._extra_clearers.pop()


if __name__ == "__main__":
    unittest.main()
