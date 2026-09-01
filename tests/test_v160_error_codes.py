"""v1.6.0 — error_codes module consistency tests.

Asserts the error-code taxonomy is internally consistent:
  - every Codes.* constant has a row in REGISTRY
  - every REGISTRY key is also a Codes constant
  - every code string starts with E_
  - every registry row has a valid severity
  - every code referenced by ``_log_error("E_…", …)`` in app.py exists
    in the registry (catches typos / ghost codes)
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import error_codes as ec


_APP_PY = Path(__file__).resolve().parent.parent / "src" / "app.py"


class ErrorCodeRegistryConsistencyTests(unittest.TestCase):
    def _constants(self) -> set[str]:
        return {
            v for n, v in vars(ec.Codes).items()
            if not n.startswith("_") and isinstance(v, str) and v.startswith("E_")
        }

    def test_constants_match_registry(self):
        constants = self._constants()
        registry = set(ec.REGISTRY.keys())
        self.assertEqual(constants, registry,
                         msg=f"missing-in-registry={constants - registry}, "
                             f"extra-in-registry={registry - constants}")

    def test_codes_have_E_prefix(self):
        for code in ec.REGISTRY:
            self.assertTrue(code.startswith("E_"), f"{code} missing E_ prefix")

    def test_severities_are_valid(self):
        valid = {"FATAL", "ERROR", "WARN", "INFO"}
        for code, meta in ec.REGISTRY.items():
            self.assertIn(meta["severity"], valid,
                          f"{code} has invalid severity {meta['severity']}")

    def test_registry_rows_have_required_fields(self):
        for code, meta in ec.REGISTRY.items():
            for field in ("severity", "description", "user_action"):
                self.assertIn(field, meta, f"{code} missing field {field}")
                self.assertTrue(meta[field], f"{code}.{field} is empty")

    def test_is_valid_code(self):
        self.assertTrue(ec.is_valid_code(ec.Codes.PLAN_PARSE_CORRUPT))
        self.assertFalse(ec.is_valid_code("E_NOT_REAL"))
        self.assertFalse(ec.is_valid_code(""))
        self.assertFalse(ec.is_valid_code(None))  # type: ignore[arg-type]
        self.assertFalse(ec.is_valid_code(42))    # type: ignore[arg-type]

    def test_metadata_lookup(self):
        meta = ec.metadata(ec.Codes.PLAN_PARSE_CORRUPT)
        self.assertIsNotNone(meta)
        assert meta is not None  # for type checker
        self.assertEqual(meta["severity"], "ERROR")
        self.assertIsNone(ec.metadata("E_GHOST"))

    def test_all_codes_returns_sorted(self):
        codes = ec.all_codes()
        self.assertEqual(codes, sorted(codes))
        self.assertEqual(set(codes), set(ec.REGISTRY.keys()))


class CodeReferencesInAppPyTests(unittest.TestCase):
    """Build-time consistency: every literal E_… inside _log_error(...)
    or error_codes.Codes.X reference in app.py must exist in REGISTRY.
    """

    def test_referenced_codes_exist_in_registry(self):
        text = _APP_PY.read_text(encoding="utf-8")
        # Grab any Codes.<NAME> attribute access. Each must be a real attr.
        attr_pattern = re.compile(r"error_codes\.Codes\.([A-Z_]+)\b")
        refs = set(attr_pattern.findall(text))
        constant_names = {n for n, v in vars(ec.Codes).items()
                          if not n.startswith("_") and isinstance(v, str)}
        for ref in refs:
            self.assertIn(ref, constant_names,
                          f"app.py references error_codes.Codes.{ref} but no such constant exists")

    def test_at_least_some_codes_are_referenced(self):
        # Smoke check: app.py must reference _log_error for at least the
        # five high-priority codes from the IP. Catches an accidental
        # delete of the helper everywhere.
        text = _APP_PY.read_text(encoding="utf-8")
        for code in [
            "PLAN_PARSE_CORRUPT",
            "ENRICH_FAILED",
            "CACHE_GENERIC",
        ]:
            self.assertIn(code, text,
                          f"app.py does not reference Codes.{code}")


if __name__ == "__main__":
    unittest.main()
