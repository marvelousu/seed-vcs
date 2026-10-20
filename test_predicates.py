"""Tests for versioned directory predicates in conditional proposals."""
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from seed import (Conflict, MAX_RANGES, SeedError, Store, V2_WRITER_GUARD,
                  canonical, normalize_prefix, object_id)


ROOT = Path(__file__).resolve().parent


class RangePredicateTests(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT / "artifacts" / "tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.store = Store.create(Path(self.temporary.name) / "repo.db")
        self.genesis = self.store.head()

    def tearDown(self):
        self.temporary.cleanup()

    def commit(self, edits, **kwargs):
        proposal = self.store.propose(self.store.head(), edits, **kwargs)
        return self.store.land(proposal)["revision"]

    def test_normalizes_prefix_and_pins_a_complete_snapshot(self):
        base = self.commit({"src/a.py": b"a", "src0/not.py": b"no", "elsewhere": b"x"})
        snapshot = self.store.range_snapshot("src", "membership", base)
        self.assertEqual(snapshot["prefix"], "src/")
        self.assertEqual(snapshot["paths"], ["src/a.py"])
        self.assertEqual(set(snapshot["files"]), {"src/a.py"})
        self.commit({"src/b.py": b"b"})
        self.assertEqual(self.store.range_snapshot("src", revision=base), snapshot)
        for invalid in (None, 3, "/src", "src//", "src/../other", "src\\child", "./src"):
            with self.subTest(invalid=invalid), self.assertRaises(SeedError):
                normalize_prefix(invalid)
        self.assertEqual(normalize_prefix(""), "")

    def test_membership_detects_new_member_but_not_outside_changes(self):
        base = self.commit({"src/a": b"a", "elsewhere": b"old"})
        rejected = self.store.propose(
            base, {"result": b"from listing"},
            ranges=[{"kind": "membership", "prefix": "src"}],
        )
        self.commit({"src/new": b"new"})
        with self.assertRaises(Conflict) as failure:
            self.store.land(rejected)
        self.assertIn("@range:membership:src/", failure.exception.differences)
        self.assertEqual(failure.exception.differences["@range:membership:src/"]["kind"],
                         "membership")
        self.assertEqual(failure.exception.head, self.store.head())

        base = self.store.head()
        accepted = self.store.propose(
            base, {"result-two": b"outside is fine"},
            ranges=[{"kind": "membership", "prefix": "src/"}],
        )
        self.commit({"elsewhere": b"new"})
        self.assertEqual(self.store.land(accepted)["status"], "landed")

    def test_content_conflicts_keep_the_file_key_and_report_kind(self):
        base = self.commit({"dependency": b"old"})
        proposal = self.store.propose(base, {"result": b"derived"}, reads=["dependency"])
        self.commit({"dependency": b"new"})
        with self.assertRaises(Conflict) as failure:
            self.store.land(proposal)
        self.assertEqual(set(failure.exception.differences), {"dependency"})
        self.assertEqual(failure.exception.differences["dependency"]["kind"], "content")

    def test_membership_ignores_contents_and_subtree_detects_them(self):
        base = self.commit({"src/a": b"old"})
        membership = self.store.propose(
            base, {"membership-result": b"ok"},
            ranges=[{"kind": "membership", "prefix": "src"}],
        )
        self.commit({"src/a": b"new"})
        self.store.land(membership)

        base = self.store.head()
        subtree = self.store.propose(
            base, {"subtree-result": b"must retry"},
            ranges=[{"kind": "subtree", "prefix": "src"}],
        )
        self.commit({"src/a": b"newer"})
        with self.assertRaises(Conflict) as failure:
            self.store.land(subtree)
        self.assertEqual(failure.exception.differences["@range:subtree:src/"]["kind"], "subtree")

    def test_subtree_detects_new_member_and_root_covers_everything(self):
        base = self.commit({"src/a": b"a", "other": b"one"})
        subtree = self.store.propose(
            base, {"subtree-result": b"must retry"},
            ranges=[{"kind": "subtree", "prefix": "src"}],
        )
        self.commit({"src/new": b"new"})
        with self.assertRaises(Conflict):
            self.store.land(subtree)

        base = self.store.head()
        root = self.store.propose(
            base, {"root-result": b"must retry"},
            ranges=[{"kind": "membership", "prefix": ""}],
        )
        self.commit({"outside": b"also a member"})
        with self.assertRaises(Conflict) as failure:
            self.store.land(root)
        self.assertIn("@range:membership:", failure.exception.differences)

    def test_unicode_and_wildcard_prefixes_are_literal(self):
        base = self.commit({"資料/要約": b"yes", "[draft]/*notes": b"yes", "draft/x": b"no"})
        snapshot = self.store.range_snapshot("資料", "subtree", base)
        self.assertEqual(snapshot["paths"], ["資料/要約"])
        wildcard = self.store.range_snapshot("[draft]", revision=base)
        self.assertEqual(wildcard["paths"], ["[draft]/*notes"])

    def test_cap_fails_instead_of_returning_a_partial_range(self):
        self.commit({f"src/{number}": b"x" for number in range(3)})
        with patch("seed.MAX_RANGE_FILES", 2), self.assertRaises(SeedError):
            self.store.range_snapshot("src")

    def test_range_count_and_noop_write_keep_explicit_read_predicate(self):
        base = self.commit({"same": b"unchanged"})
        proposal = self.store.propose(
            base, {"same": b"unchanged", "result": b"derived"}, reads=["same"],
        )
        self.assertIn("same", self.store.describe(proposal)["expected"])
        too_many = [{"kind": "membership", "prefix": f"p{number}"}
                    for number in range(MAX_RANGES + 1)]
        with self.assertRaises(SeedError):
            self.store.propose(base, {"overflow": b"x"}, ranges=too_many)

    def test_format_one_identity_stays_stable_and_format_two_supersedes(self):
        legacy = self.store.propose(self.genesis, {"legacy": b"one"}, reads=["missing"])
        self.assertEqual(
            legacy,
            self.store.propose(self.genesis, {"legacy": b"one"}, reads=["missing"]),
        )
        self.assertEqual(self.store.describe(legacy)["format"], 1)
        with closing(sqlite3.connect(self.store.database)) as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='format'").fetchone()[0],
                             "seed/1")
        replacement = self.store.propose(
            self.genesis, {"replacement": b"two"}, supersedes=legacy,
        )
        body = self.store.describe(replacement)
        self.assertEqual(body["format"], 2)
        self.assertEqual(body["supersedes"], legacy)
        with closing(sqlite3.connect(self.store.database)) as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='format'").fetchone()[0],
                             "seed/2")
        self.assertEqual(Store(self.store.database).head(), self.genesis)
        self.assertEqual(
            self.store.proposal_status(replacement),
            {"proposal": replacement, "status": "pending", "revision": None,
             "head": self.genesis},
        )
        landed = self.store.land(replacement)
        self.assertEqual(self.store.proposal_status(replacement)["revision"], landed["revision"])
        with self.assertRaises(SeedError):
            self.store.propose(self.store.head(), {"missing-parent": b"x"}, supersedes="0" * 64)

    def test_unknown_proposal_format_is_rejected(self):
        body = canonical({"format": 99})
        proposal = object_id("proposal", body)
        with closing(sqlite3.connect(self.store.database)) as db:
            db.execute("INSERT INTO proposals VALUES (?, ?)", (proposal, body))
            db.commit()
        with self.assertRaisesRegex(SeedError, "Unsupported proposal format"):
            self.store.describe(proposal)

    def test_old_writer_expected_map_cannot_accept_a_format_two_proposal(self):
        proposal = self.store.propose(self.genesis, {"output": b"value"},
                                      ranges=[{"kind": "subtree", "prefix": "src/"}])
        body = self.store.describe(proposal)
        self.assertEqual(body["expected"][V2_WRITER_GUARD], {"requires": 2})
        # Seed 0.2 checked each expected entry without understanding format 2.
        # Even a cached old PostgreSQL writer must reject, not skip new guards.
        with self.store._db() as db:
            current = self.store._current_many(db, body["expected"])
        self.assertFalse(all(current[path] == expected
                             for path, expected in body["expected"].items()))
        self.assertEqual(self.store.land(proposal)["status"], "landed")

    def test_format_two_requires_an_explicit_ranges_field(self):
        proposal = self.store.propose(self.genesis, {"output": b"value"},
                                      ranges=[{"kind": "subtree", "prefix": "src/"}])
        body = self.store.describe(proposal)
        del body["ranges"]
        payload = canonical(body)
        malformed = object_id("proposal", payload)
        with closing(sqlite3.connect(self.store.database)) as db:
            db.execute("INSERT INTO proposals VALUES (?, ?)", (malformed, payload))
            db.commit()
        with self.assertRaisesRegex(SeedError, "Unsupported proposal format"):
            self.store.land(malformed)
        self.assertEqual(self.store.head(), self.genesis)
        self.assertIsNone(self.store.read("output"))


if __name__ == "__main__":
    unittest.main()
