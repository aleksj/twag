import unittest
from unittest.mock import patch

from twag_clickhouse.cloud import ClickHouseCloudClient, ClickHouseCloudConfig


class ClickHouseCloudClientTests(unittest.TestCase):
    def test_connection_defaults_prefers_https_endpoint(self):
        config = ClickHouseCloudConfig(
            service_id="test-service-id",
            key_id="key-id",
            key_secret="secret",
            organization_id="org-id",
        )
        client = ClickHouseCloudClient(config)
        service = {
            "endpoints": [
                {"protocol": "native", "host": "native.example.com", "port": 9440},
                {
                    "protocol": "https",
                    "host": "https.example.com",
                    "port": 8443,
                    "username": "default",
                },
            ]
        }

        with patch.object(client, "get_service", return_value=service):
            resolved = client.connection_defaults()

        self.assertEqual(resolved["service_id"], config.service_id)
        self.assertEqual(resolved["host"], "https.example.com")
        self.assertEqual(resolved["port"], 8443)
        self.assertEqual(resolved["username"], "default")


if __name__ == "__main__":
    unittest.main()
