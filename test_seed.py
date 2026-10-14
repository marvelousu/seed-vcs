"""Behavioral tests of publication, history, concurrency, and stated limits."""
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
import multiprocessing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from seed import Conflict, SeedError, Store


ROOT = Path(__file__).resolve().parent


def concurrent_land(arguments):
    database, proposal = arguments
    try:
        return Store(database).land(proposal)["status"]
    except Conflict:
        return "conflict"


class SeedTests(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT / "artifacts" / "tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.root = Path(self.temporary.name)
        self.store = Store.create(self.root / "repo.db")
        self.genesis = self.store.head()

    def tearDown(self):
        self.temporary.cleanup()

    def commit(self, edits, **kwargs):
        proposal = self.store.propose(self.store.head(), edits, **kwargs)
        return self.store.land(proposal)["revision"]

    def processes(self, proposals):
        with ProcessPoolExecutor(max_workers=4,
                                 mp_context=multiprocessing.get_context("spawn")) as pool:
            return list(pool.map(concurrent_land,
                                 [(str(self.store.database), p) for p in proposals]))

    def test_independent_changes_commute_in_state_not_history(self):
        a = self.store.propose(self.genesis, {"a.py": b"A"})
        b = self.store.propose(self.genesis, {"b.py": b"B"})
        self.store.land(a)
        self.store.land(b)
        second = Store.create(self.root / "other.db")
        a2 = second.propose(second.head(), {"a.py": b"A"})
        b2 = second.propose(second.head(), {"b.py": b"B"})
        self.assertEqual((a, b), (a2, b2))
        second.land(b2)
        second.land(a2)
        self.assertEqual(self.store.manifest(), second.manifest())
        self.assertNotEqual(self.store.head(), second.head())

    def test_conflict_refuses_whole_package(self):
        base = self.commit({"a": b"old", "b": b"old"})
        proposal = self.store.propose(base, {"a": b"mine", "b": b"mine"})
        winner = self.commit({"a": b"theirs"})
        history = self.store.log()
        with self.assertRaises(Conflict) as failure:
            self.store.land(proposal)
        self.assertEqual(set(failure.exception.differences), {"a"})
        self.assertEqual(self.store.head(), winner)
        self.assertEqual(self.store.log(), history)
        self.assertEqual(self.store.read("b"), b"old")

    def test_declared_read_blocks_changed_dependency(self):
        base = self.commit({"api.py": b"def old(): pass"})
        caller = self.store.propose(base, {"caller.py": b"old()"}, reads=["api.py"])
        self.commit({"api.py": b"def new(): pass"})
        with self.assertRaises(Conflict) as failure:
            self.store.land(caller)
        self.assertIn("api.py", failure.exception.differences)
        self.assertIsNone(self.store.read("caller.py"))

    def test_declared_absence_blocks_new_exact_path(self):
        proposal = self.store.propose(self.genesis, {"result": b"no config"}, reads=["config"])
        self.commit({"config": b"now exists"})
        with self.assertRaises(Conflict):
            self.store.land(proposal)

    def test_undeclared_dependency_is_not_detected(self):
        base = self.commit({"api.py": b"def old(): return 1"})
        caller = self.store.propose(base, {"caller.py": b"answer = old()"})
        self.commit({"api.py": b"def new(): return 1"})
        self.store.land(caller)
        with self.assertRaises(NameError):
            exec(self.store.read("api.py") + b"\n" + self.store.read("caller.py"), {})

    def test_directory_listing_phantom_is_not_detected(self):
        base = self.commit({"plugins/a": b"plugin A"})
        proposal = self.store.propose(base, {"index": b"plugins/a"}, reads=["plugins/a"])
        self.commit({"plugins/b": b"plugin B"})
        self.store.land(proposal)
        self.assertEqual(self.store.read("index"), b"plugins/a")
        self.assertIn("plugins/b", self.store.manifest())

    def test_history_empty_bytes_deletion_and_recreation(self):
        first = self.commit({"file": b"\0\xff"})
        deleted = self.commit({"file": None})
        recreated = self.commit({"file": b""})
        self.assertIsNone(self.store.read("file", self.genesis))
        self.assertEqual(self.store.read("file", first), b"\0\xff")
        self.assertIsNone(self.store.read("file", deleted))
        self.assertEqual(self.store.manifest(deleted), {})
        self.assertEqual(self.store.read("file", recreated), b"")
        self.assertIn("file", self.store.manifest(recreated))

    def test_rename_checks_destination_before_deleting_source(self):
        base = self.commit({"old": b"value"})
        rename = self.store.propose(base, {"old": None, "new": b"value"})
        self.commit({"new": b"someone else"})
        with self.assertRaises(Conflict):
            self.store.land(rename)
        self.assertEqual(self.store.read("old"), b"value")
        self.assertEqual(self.store.read("new"), b"someone else")
        moved = self.store.propose(self.store.head(), {"old": None, "other": b"value"})
        self.store.land(moved)
        self.assertIsNone(self.store.read("old"))
        self.assertEqual(self.store.read("other"), b"value")

    def test_namespace_collision_rolls_back_everything(self):
        base = self.commit({"pkg": b"file"})
        invalid = self.store.propose(base, {"pkg/config": b"child", "other": b"write"})
        with self.assertRaises(SeedError):
            self.store.land(invalid)
        self.assertEqual(self.store.head(), base)
        self.assertEqual(set(self.store.manifest()), {"pkg"})
        second = Store.create(self.root / "collision.db")
        p = second.propose(second.head(), {"a": b"parent", "a/b": b"child"})
        with self.assertRaises(SeedError):
            second.land(p)
        self.assertEqual(second.manifest(), {})

    def test_namespace_check_handles_new_ancestor_of_existing_file(self):
        base = self.commit({"pkg/config": b"child"})
        invalid = self.store.propose(base, {"pkg": b"parent"})
        with self.assertRaises(SeedError):
            self.store.land(invalid)
        self.assertEqual(set(self.store.manifest()), {"pkg/config"})

    def test_file_to_directory_and_back_is_atomic(self):
        self.commit({"pkg": b"file"})
        self.commit({"pkg": None, "pkg/config": b"child"})
        self.assertEqual(set(self.store.manifest()), {"pkg/config"})
        self.commit({"pkg/config": None, "pkg": b"new file"})
        self.assertEqual(set(self.store.manifest()), {"pkg"})

    def test_noops_removed_and_empty_package_refused(self):
        base = self.commit({"same": b"unchanged"})
        with self.assertRaises(SeedError):
            self.store.propose(base, {"same": b"unchanged", "absent": None})
        proposal = self.store.propose(base, {"same": b"unchanged", "new": b"change"})
        self.assertNotIn("same", self.store.describe(proposal)["expected"])
        self.commit({"same": b"later"})
        self.store.land(proposal)
        self.assertEqual(self.store.read("same"), b"later")

    def test_proposal_identity_and_idempotent_retry_after_head_moves(self):
        first = self.store.propose(self.genesis, {"b": b"2", "a": b"1"}, reads=["x", "y"])
        again = self.store.propose(self.genesis, {"a": b"1", "b": b"2"}, reads=["y", "x"])
        self.assertEqual(first, again)
        result = self.store.land(first)
        self.commit({"unrelated": b"new"})
        head = self.store.head()
        count = len(self.store.log())
        retried = self.store.land(first)
        self.assertEqual(retried["revision"], result["revision"])
        self.assertEqual(retried["status"], "already-landed")
        self.assertEqual(self.store.head(), head)
        self.assertEqual(len(self.store.log()), count)

    def test_content_predicate_allows_aba_and_retry_after_conflict(self):
        base = self.commit({"file": b"A"})
        proposal = self.store.propose(base, {"file": b"C"})
        self.commit({"file": b"B"})
        with self.assertRaises(Conflict):
            self.store.land(proposal)
        self.commit({"file": b"A"})
        self.store.land(proposal)
        self.assertEqual(self.store.read("file"), b"C")

    def test_storage_failure_rolls_back_files_history_and_head(self):
        base = self.commit({"file": b"old"})
        proposal = self.store.propose(base, {"file": b"new", "extra": b"new"})
        with closing(sqlite3.connect(self.store.database, isolation_level=None)) as db:
            db.execute("CREATE TRIGGER fail_history BEFORE INSERT ON versions "
                       "BEGIN SELECT RAISE(ABORT, 'injected history failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.land(proposal)
        reopened = Store(self.store.database)
        self.assertEqual(reopened.head(), base)
        self.assertEqual(reopened.read("file"), b"old")
        self.assertIsNone(reopened.read("extra"))
        self.assertEqual(len(reopened.log()), 2)

    def test_damaged_blob_is_refused_before_publication(self):
        proposal = self.store.propose(self.genesis, {"file": b"good"})
        with closing(sqlite3.connect(self.store.database, isolation_level=None)) as db:
            db.execute("UPDATE blobs SET payload=?", (b"bad",))
        with self.assertRaises(SeedError):
            self.store.land(proposal)
        self.assertEqual(self.store.head(), self.genesis)
        self.assertEqual(self.store.manifest(), {})

    def test_damaged_proposal_is_refused(self):
        proposal = self.store.propose(self.genesis, {"file": b"good"})
        with closing(sqlite3.connect(self.store.database, isolation_level=None)) as db:
            db.execute("UPDATE proposals SET payload=?", (b"{}",))
        with self.assertRaises(SeedError):
            self.store.land(proposal)

    def test_paths_unknown_revision_and_existing_init(self):
        for invalid in ("", "/absolute", "a/../b", "a//b", "a/./b", "a/", "a\\b", "a\0b"):
            with self.subTest(path=invalid), self.assertRaises(SeedError):
                self.store.propose(self.genesis, {invalid: b"data"})
        with self.assertRaises(SeedError):
            self.store.read("file", "unknown")
        with self.assertRaises(SeedError):
            Store.create(self.store.database)
        self.assertEqual(self.store.head(), self.genesis)

    def test_processes_do_not_lose_independent_updates(self):
        proposals = [self.store.propose(self.genesis, {f"workers/{i}": str(i).encode()})
                     for i in range(24)]
        self.assertEqual(self.processes(proposals), ["landed"] * len(proposals))
        self.assertEqual(len(self.store.manifest()), len(proposals))
        self.assertEqual(len(self.store.log()), len(proposals) + 1)
        for i in range(24):
            self.assertEqual(self.store.read(f"workers/{i}"), str(i).encode())

    def test_processes_same_proposal_create_one_revision(self):
        proposal = self.store.propose(self.genesis, {"file": b"value"})
        statuses = self.processes([proposal] * 12)
        self.assertEqual(statuses.count("landed"), 1)
        self.assertEqual(statuses.count("already-landed"), 11)
        self.assertEqual(len(self.store.log()), 2)

    def test_processes_overlap_have_one_winner(self):
        proposals = [self.store.propose(self.genesis, {"file": str(i).encode()})
                     for i in range(12)]
        statuses = self.processes(proposals)
        self.assertEqual(statuses.count("landed"), 1)
        self.assertEqual(statuses.count("conflict"), 11)
        self.assertEqual(len(self.store.log()), 2)

    def test_cli_binary_roundtrip_and_conflict_exit(self):
        def run(*args):
            return subprocess.run([sys.executable, str(ROOT / "seed.py"), "--db",
                                   str(self.store.database), *args], capture_output=True)
        payload = self.root / "payload.bin"
        payload.write_bytes(b"\0\xff\n")
        proposed = run("propose", "--base", self.genesis, "--set", f"file={payload}")
        self.assertEqual(proposed.returncode, 0, proposed.stderr)
        proposal = proposed.stdout.decode().strip()
        self.assertEqual(run("land", proposal).returncode, 0)
        cat = run("cat", "file")
        self.assertEqual(cat.returncode, 0, cat.stderr)
        self.assertEqual(cat.stdout, payload.read_bytes())
        conflicting = self.store.propose(self.genesis, {"file": b"different"})
        refused = run("land", conflicting)
        self.assertEqual(refused.returncode, 2)
        self.assertIn(b'"conflict"', refused.stderr)


if __name__ == "__main__":
    unittest.main()
