from pathlib import Path
import unittest

from twag_clickhouse.nytw import (
    NytwDataset,
    PLATFORM_ADMIN_ID,
    inspect_nytw_dataset,
    parse_event_file,
)


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data" / "nytw-2026-for-agents"


class NytwParserTests(unittest.TestCase):
    def test_parse_representative_event(self):
        path = DATASET / "events" / "2026-06-01-0400-4am-irr-for-the-inner-irr-nerd-in-you.md"
        event = parse_event_file(path, DATASET)

        self.assertEqual(event["event_id"], "iyeVYNaUJxZqidnHixwa")
        self.assertEqual(event["title"], "4am IRR - for the inner IRR nerd in you!")
        self.assertEqual(event["event_date"].isoformat(), "2026-06-01")
        self.assertEqual(event["start_at"].isoformat(), "2026-06-01T08:00:00+00:00")
        self.assertIn(PLATFORM_ADMIN_ID, event["owner_ids"])
        self.assertIn("Internal Rate of Return", event["description"])

    def test_inspect_dataset_counts(self):
        counts = inspect_nytw_dataset(NytwDataset.from_path(DATASET))

        self.assertEqual(counts["event_files"], 1385)
        self.assertEqual(counts["events"], 1385)
        self.assertEqual(counts["hosts"], 2047)
        self.assertGreater(counts["event_hosts"], counts["events"])
        self.assertEqual(counts["manifest"], 1382)


if __name__ == "__main__":
    unittest.main()
