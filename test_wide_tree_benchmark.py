from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import wide_tree_benchmark as wide


class WideTreeBenchmarkTests(unittest.TestCase):
    def test_tiny_positive_and_stale_control_oracle(self):
        wide.TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=wide.TMP) as temporary:
            result = wide.run(widths=[8], batches=[2], repeats=1, proposals=4,
                              temporary_parent=Path(temporary))
        self.assertEqual(result["format"], wide.FORMAT)
        self.assertEqual(len(result["rows"]), 4)  # flat and sharded, Seed and Git
        self.assertEqual(len(result["preparation"]), 2)
        for row in result["rows"]:
            self.assertEqual([item["proposal"] for item in row["decisions"]], [0, 1, 2, 3])
            self.assertEqual([item["accepted"] for item in row["decisions"]], [False, True, True, True])
            self.assertEqual(row["accepted"], 3)
            self.assertIn("before_publication", row["storage"])
            self.assertIn("setup_seconds", row["metrics"])
        by_layout = {}
        for row in result["rows"]:
            by_layout.setdefault(row["layout"], []).append(row)
        for rows in by_layout.values():
            self.assertEqual(rows[0]["final_digest"], rows[1]["final_digest"])

    def test_dimension_bounds_and_duplicates_fail(self):
        with self.assertRaises(ValueError):
            wide.run(widths=[8, 8], batches=[1], repeats=1, proposals=4)
        with self.assertRaises(ValueError):
            wide.run(widths=[8], batches=[1, 1], repeats=1, proposals=4)
        with self.assertRaises(ValueError):
            wide.run(widths=[3], batches=[1], repeats=1, proposals=4)
        with self.assertRaises(ValueError):
            wide.run(widths=[8], batches=[5], repeats=1, proposals=4)


if __name__ == "__main__":
    unittest.main()
