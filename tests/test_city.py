import os
import unittest
from unittest import mock

from twag_clickhouse.city import BOSTON, NYC, active_city, load_city


class CityResolverTests(unittest.TestCase):
    def test_default_resolves_to_nyc(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TWAG_CITY", None)
            city = active_city()
        self.assertEqual(city.slug, "nyc")
        self.assertEqual(city.table_prefix, "nytw")
        self.assertEqual(city.dataset_path, "data/nytw-2026-for-agents")

    def test_boston_resolves(self):
        with mock.patch.dict(os.environ, {"TWAG_CITY": "boston"}):
            city = active_city()
        self.assertEqual(city.slug, "boston")
        self.assertEqual(city.table_prefix, "bostw")
        self.assertEqual(city.tool_name, "query_bostw_clickhouse")
        self.assertEqual(city.dataset_path, "data/bostontw-2026-for-agents")
        self.assertIn("Boston", city.display_name)

    def test_unknown_city_raises(self):
        with self.assertRaises(ValueError):
            load_city("springfield")

    def test_registry_holds_both_cities(self):
        self.assertIs(load_city("nyc"), NYC)
        self.assertIs(load_city("boston"), BOSTON)


if __name__ == "__main__":
    unittest.main()
