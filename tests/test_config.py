import os
import unittest
from unittest.mock import patch

from twag_clickhouse.config import ClickHouseConfig


class ClickHouseConfigTests(unittest.TestCase):
    def test_from_env_uses_api_key_alias(self):
        env = {
            "CLICKHOUSE_HOST": "example.clickhouse.cloud",
            "CLICKHOUSE_USERNAME": "default",
            "CLICKHOUSE_API_KEY": "secret-key",
            "CLICKHOUSE_SECURE": "true",
        }

        with patch.dict(os.environ, env, clear=True):
            config = ClickHouseConfig.from_env(env_file=None)

        self.assertEqual(config.host, "example.clickhouse.cloud")
        self.assertEqual(config.password, "secret-key")
        self.assertEqual(config.port, 8443)
        self.assertTrue(config.secure)

    def test_safe_dict_masks_password(self):
        config = ClickHouseConfig(
            host="localhost",
            username="default",
            password="secret",
            secure=False,
            port=8123,
        )

        self.assertEqual(config.safe_dict()["password"], "***")


if __name__ == "__main__":
    unittest.main()
