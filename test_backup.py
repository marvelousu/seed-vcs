"""Logical archive contracts for SQLite, plus an opt-in PostgreSQL smoke test."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import base64
import json
import subprocess
import tempfile
import unittest

from seed import SeedError, Store, canonical, object_id
import seed_backup
from seed_backup import backup, restore, verify_archive
from seed_validation import (candidate, create_candidate, promote, publish_candidate,
                             record_validation, set_policy, validation_state)


ROOT = Path(__file__).resolve().parent


class BackupTests(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT / "artifacts" / "tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.root = Path(self.temporary.name)
        self.source = Store.create(self.root / "source.db")

    def tearDown(self):
        self.temporary.cleanup()

    def commit(self, edits):
        proposal = self.source.propose(self.source.head(), edits)
        return self.source.land(proposal)["revision"]

    def rewrite_manifest(self, archive, manifest):
        """Intentionally rewrite a test fixture while retaining canonical JSON."""
        destination = archive / "manifest.json"
        temporary = archive / "manifest.rewrite"
        temporary.write_bytes(canonical(manifest))
        os.replace(temporary, destination)

    def make_junction(self, link, target):
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            self.skipTest("Windows junction creation is unavailable: " + result.stderr)

    def archive_with_history_and_pending_proposal(self):
        first = self.commit({"history": b"first", "removed": b"gone"})
        self.commit({"history": b"second", "removed": None})

        configuration = "1" * 64
        set_policy(self.source, [configuration])
        candidate_proposal = self.source.propose(
            self.source.head(), {"candidate": b"published"})
        candidate_info = create_candidate(self.source, [candidate_proposal])
        validation = record_validation(
            self.source, kind="candidate", subject=candidate_info["candidate"],
            configuration=configuration, environment="2" * 64, passed=True,
            returncode=0, log_digest="3" * 64, request="backup-fixture")
        publish_candidate(self.source, candidate_info["candidate"])
        promotion = promote(self.source, candidate_info["revision"])
        pending = self.source.propose(self.source.head(), {"pending": b"not landed"})
        archive = self.root / "archive"
        backup(self.source, archive)
        return archive, first, pending, candidate_info, validation, promotion

    def test_backup_restore_preserves_history_pending_proposal_and_meta(self):
        (archive, first, pending, candidate_info, validation,
         promotion) = self.archive_with_history_and_pending_proposal()
        manifest = verify_archive(archive)
        self.assertEqual(dict(manifest["meta"])["head"], self.source.head())
        self.assertEqual(len(manifest["proposals"]), 4)

        target_path = self.root / "target.db"
        target = Store.create(target_path)
        restored = restore(target, archive)
        reopened = Store(target_path)
        self.assertEqual(restored["head"], self.source.head())
        self.assertEqual(reopened.manifest(), self.source.manifest())
        self.assertEqual(reopened.read("history", first), b"first")
        self.assertEqual(reopened.read("history"), b"second")
        self.assertIsNone(reopened.read("removed"))
        self.assertEqual(reopened.proposal_status(pending)["status"], "pending")
        self.assertEqual(candidate(reopened, candidate_info["candidate"]), candidate_info)
        source_state = validation_state(self.source)
        self.assertEqual(validation_state(reopened), source_state)
        self.assertEqual(source_state["policy"]["configurations"], ["1" * 64])
        self.assertEqual(source_state["verified"]["revision"], candidate_info["revision"])
        self.assertEqual(source_state["deployable"]["revision"], candidate_info["revision"])
        with reopened._db() as db:
            metadata = {row["key"]: row["value"]
                        for row in db.execute("SELECT key, value FROM meta")}
        for key in ("validation-policy", "candidate:" + candidate_info["candidate"],
                    "validation:" + validation["validation"], "verified", "deployable",
                    "promotion:" + promotion["promotion"]):
            self.assertEqual(metadata[key], dict(manifest["meta"])[key])

    def test_missing_or_corrupt_blob_fails_before_target_is_written(self):
        archive, *_ = self.archive_with_history_and_pending_proposal()
        blob = next((archive / "blobs").rglob("*"))
        while blob.is_dir():
            blob = next(blob.iterdir())
        blob.unlink()
        with self.assertRaises(SeedError):
            verify_archive(archive)
        target = Store.create(self.root / "target.db")
        with self.assertRaises(SeedError):
            restore(target, archive)
        self.assertEqual(target.manifest(), {})
        self.assertEqual(len(target.log()), 1)

    def test_restore_failure_rolls_back_sqlite_metadata_and_files(self):
        archive, *_ = self.archive_with_history_and_pending_proposal()
        target = Store.create(self.root / "target.db")
        original = target._put_blob
        calls = 0

        def fail_after_first(db, payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SeedError("injected restore failure")
            return original(db, payload)

        target._put_blob = fail_after_first
        with self.assertRaises(SeedError):
            restore(target, archive)
        self.assertEqual(target.manifest(), {})
        self.assertEqual(len(target.log()), 1)
        with target._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) AS count FROM blobs").fetchone()["count"], 0)

    def test_restore_refuses_nonempty_target_and_never_overwrites_archive(self):
        archive, *_ = self.archive_with_history_and_pending_proposal()
        with self.assertRaises(SeedError):
            backup(self.source, archive)
        target = Store.create(self.root / "target.db")
        proposal = target.propose(target.head(), {"keep": b"value"})
        target.land(proposal)
        with self.assertRaises(SeedError):
            restore(target, archive)
        self.assertEqual(target.read("keep"), b"value")

    def test_canonical_archive_rejects_versions_that_disagree_with_proposal_writes(self):
        archive, *_ = self.archive_with_history_and_pending_proposal()
        manifest = json.loads((archive / "manifest.json").read_bytes())
        revision = manifest["revisions"][-1]
        proposal = next(row for row in manifest["proposals"] if row["id"] == revision["proposal"])
        writes = json.loads(base64.b64decode(proposal["payload"]))["writes"]
        path = next(iter(writes))
        alternate = b"valid alternate blob that was never proposed"
        alternate_id = object_id("blob", alternate)
        blob_path = archive / "blobs" / alternate_id[:2] / alternate_id[2:]
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        blob_path.write_bytes(alternate)
        manifest["blobs"].append({"id": alternate_id, "size": len(alternate)})
        manifest["blobs"].sort(key=lambda row: row["id"])
        next(row for row in manifest["versions"]
             if row["seq"] == revision["seq"] and row["path"] == path)["blob"] = alternate_id
        next(row for row in manifest["current_files"] if row["path"] == path)["blob"] = alternate_id
        self.rewrite_manifest(archive, manifest)
        with self.assertRaises(SeedError):
            verify_archive(archive)

    def test_canonical_archive_rejects_proposal_with_unknown_base(self):
        archive, *_ = self.archive_with_history_and_pending_proposal()
        manifest = json.loads((archive / "manifest.json").read_bytes())
        landed = {row["proposal"] for row in manifest["revisions"] if row["proposal"]}
        proposal = next(row for row in manifest["proposals"] if row["id"] not in landed)
        body = json.loads(base64.b64decode(proposal["payload"]))
        body["base"] = "f" * 64
        payload = canonical(body)
        proposal["payload"] = base64.b64encode(payload).decode("ascii")
        proposal["id"] = object_id("proposal", payload)
        self.rewrite_manifest(archive, manifest)
        with self.assertRaises(SeedError):
            verify_archive(archive)

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_root_junction_is_rejected(self):
        archive, *_ = self.archive_with_history_and_pending_proposal()
        backing = self.root / "archive-backing"
        archive.rename(backing)
        self.make_junction(archive, backing)
        with self.assertRaises(SeedError):
            verify_archive(archive)

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_junction_is_rejected_before_any_blob_is_read(self):
        archive, *_ = self.archive_with_history_and_pending_proposal()
        blob = next(path for path in (archive / "blobs").rglob("*") if path.is_file())
        shard = blob.parent
        backing = self.root / "blob-shard-backing"
        shard.rename(backing)
        self.make_junction(shard, backing)
        original = seed_backup._regular_file

        def manifest_only(path, *, maximum):
            if path != archive / "manifest.json":
                self.fail("verification read a blob before rejecting the junction")
            return original(path, maximum=maximum)

        seed_backup._regular_file = manifest_only
        try:
            with self.assertRaises(SeedError):
                verify_archive(archive)
        finally:
            seed_backup._regular_file = original


@unittest.skipUnless(os.environ.get("SEED_TEST_DATABASE_URL"),
                     "Set SEED_TEST_DATABASE_URL for PostgreSQL backup smoke")
class PostgresBackupTests(unittest.TestCase):
    def setUp(self):
        from seed_postgres import PostgresStore, make_pool

        temporary_root = ROOT / "artifacts" / "tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.root = Path(self.temporary.name)
        self.pool = make_pool(os.environ["SEED_TEST_DATABASE_URL"])
        self.source = PostgresStore.create(self.pool, "backup_" + secrets.token_hex(8), self.root / "objects")
        self.target_name = "backup_" + secrets.token_hex(8)
        self.target = PostgresStore.create(self.pool, self.target_name, self.root / "objects")

    def tearDown(self):
        with self.pool.connection() as connection:
            connection.execute(f'DROP SCHEMA "{self.source.schema}" CASCADE')
            connection.execute(f'DROP SCHEMA "{self.target.schema}" CASCADE')
        self.pool.close()
        self.temporary.cleanup()

    def test_fresh_postgres_restore_reads_current_historical_and_pending(self):
        first = self.source.land(self.source.propose(self.source.head(), {"file": b"old"}))["revision"]
        self.source.land(self.source.propose(self.source.head(), {"file": b"new"}))
        pending = self.source.propose(self.source.head(), {"pending": b"bytes"})
        archive = self.root / "archive"
        backup(self.source, archive)
        restore(self.target, archive)
        self.assertEqual(self.target.read("file", first), b"old")
        self.assertEqual(self.target.read("file"), b"new")
        self.assertEqual(self.target.proposal_status(pending)["status"], "pending")


if __name__ == "__main__":
    unittest.main()
