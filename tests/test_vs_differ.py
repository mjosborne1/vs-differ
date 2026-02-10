import sys
import os
from pathlib import Path

# Add parent directory to path so we can import vs_differ
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from unittest.mock import Mock, patch
from vs_differ import expand_valueset_count, build_rows


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class ExpandValueSetCountTests(unittest.TestCase):
    def test_total_int_is_used(self):
        response = FakeResponse(200, {"expansion": {"total": 42}})
        with patch("requests.get", return_value=response):
            count = expand_valueset_count("https://example.com", "http://vs", "20240131")
        self.assertEqual(count, 42)

    def test_total_string_is_parsed(self):
        response = FakeResponse(200, {"expansion": {"total": "7"}})
        with patch("requests.get", return_value=response):
            count = expand_valueset_count("https://example.com", "http://vs", "20240131")
        self.assertEqual(count, 7)

    def test_contains_list_is_counted(self):
        response = FakeResponse(200, {"expansion": {"contains": [{"code": "a"}, {"code": "b"}]}})
        with patch("requests.get", return_value=response):
            count = expand_valueset_count("https://example.com", "http://vs", "20240131")
        self.assertEqual(count, 2)

    def test_unexpected_expansion_returns_none(self):
        response = FakeResponse(200, {"expansion": {"contains": "not-a-list"}})
        with patch("requests.get", return_value=response):
            with self.assertLogs(level="WARNING"):
                count = expand_valueset_count(
                    "https://example.com", "http://vs", "20240131"
                )
        self.assertIsNone(count)


class BuildRowsTests(unittest.TestCase):
    def test_only_ncts_valuesets_are_included(self):
        deduped = [
            {
                "valueset_url": "http://healthterminologies.gov.au/valueset/test",
                "structure_definition_url": "http://example.org/StructureDefinition/One",
                "structure_definition_name": "One",
            },
            {
                "valueset_url": "http://example.org/valueset/other",
                "structure_definition_url": "http://example.org/StructureDefinition/Two",
                "structure_definition_name": "Two",
            },
        ]
        valueset_index = {
            "http://healthterminologies.gov.au/valueset/test": {"name": "Test VS"},
            "http://example.org/valueset/other": {"name": "Other VS"},
        }

        rows = build_rows(deduped, valueset_index, [], "https://example.com")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["valueset_url"], "http://healthterminologies.gov.au/valueset/test")


class IntegrationTests(unittest.TestCase):
    """Integration tests that make real HTTP requests to terminology servers."""

    def test_expand_real_ncts_valueset(self):
        """Test expansion of a real NCTS valueset.
        
        This test makes a real HTTP request to the NCTS terminology server.
        Skip this test if the server is unavailable or you want faster unit tests.
        """
        endpoint = "https://tx.ontoserver.csiro.au/fhir"
        valueset_url = "https://healthterminologies.gov.au/fhir/ValueSet/healthcare-organisation-role-type-1"
        snomed_version = "20250531"  # Recent SNOMED AU version
        
        count = expand_valueset_count(endpoint, valueset_url, snomed_version)
        print(f"count is {count}\n")
        # The count should be a positive integer
        self.assertIsNotNone(count, "Expansion should return a count")
        assert count is not None  # Type narrowing for Pylance
        self.assertIsInstance(count, int, "Count should be an integer")
        self.assertGreater(count, 0, "Count should be positive")


if __name__ == "__main__":
    unittest.main()
