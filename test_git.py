"""End-to-end contracts for the intentionally one-way Seed/Git bridge."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from seed import SeedError, Store
from seed_git import export_seed_revision, import_git_commit


ROOT = Path(__file__).resolve().parent


def git(root: Path, *arguments: str, input_data: bytes | None = None, check: bool = True,
        environment: dict[str, str] | None = None) -> bytes:
    options = {} if input_data is None else {"input": input_data}
    env = os.environ.copy()
    if environment:
        env.update(environment)
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        env=env,
        timeout=20,
        **options,
    )
    if check and result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return result.stdout


def initialize(root: Path) -> None:
    git(root.parent, "init", "-q", "--initial-branch=main", str(root))
    git(root, "config", "user.name", "Test User")
    git(root, "config", "user.email", "test@example.invalid")


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "-A")
    tree = git(root, "write-tree").decode().strip()
    parent = git(root, "rev-parse", "--verify", "--quiet", "HEAD", check=False).decode().strip()
    arguments = ["commit-tree", tree]
    if parent:
        arguments.extend(["-p", parent])
    identity = {
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
    }
    commit = git(root, *arguments, "-m", message, environment=identity).decode().strip()
    git(root, "update-ref", "refs/heads/main", commit)
    return commit


class GitBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT / "artifacts" / "tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.destination = self.root / "destination"
        initialize(self.source)
        initialize(self.destination)
        (self.source / "binary.bin").write_bytes(b"\x00\xffgit\n")
        (self.source / "資料").mkdir()
        (self.source / "資料" / "挨拶.txt").write_text("こんにちは\n", encoding="utf-8")
        (self.source / "plain.txt").write_text("plain\n", encoding="utf-8")
        self.commit = commit_all(self.source, "source")
        self.store = Store.create(self.root / "seed.db")

    def tearDown(self):
        self.temporary.cleanup()

    def test_import_export_binary_unicode_delete_and_retry(self):
        imported = import_git_commit(self.store, self.source, self.commit)
        revision = imported["revision"]
        self.assertEqual(imported["status"], "landed")
        self.assertEqual(self.store.read("binary.bin"), b"\x00\xffgit\n")
        self.assertEqual(self.store.read("資料/挨拶.txt"), "こんにちは\n".encode())
        repeated_import = import_git_commit(self.store, self.source, self.commit)
        self.assertEqual(repeated_import["status"], "already-imported")
        self.assertEqual(repeated_import["revision"], revision)

        # Export must leave a user's checked-out worktree untouched.
        (self.destination / "user.txt").write_text("keep me\n", encoding="utf-8")
        first = export_seed_revision(self.store, self.destination, revision, branch="seed/pilot")
        self.assertEqual(first["status"], "exported")
        self.assertEqual((self.destination / "user.txt").read_text(encoding="utf-8"), "keep me\n")
        self.assertEqual(git(self.destination, "status", "--porcelain"), b"?? user.txt\n")
        self.assertEqual(git(self.destination, "show", first["commit"] + ":binary.bin"), b"\x00\xffgit\n")
        self.assertEqual(
            git(self.destination, "show", first["commit"] + ":資料/挨拶.txt"),
            "こんにちは\n".encode(),
        )

        # Simulate termination after the branch CAS but before the mapping ref.
        git(self.destination, "update-ref", "-d", "refs/seed/exports/" + revision)
        recovered = export_seed_revision(self.store, self.destination, revision, branch="seed/pilot")
        self.assertEqual(recovered["status"], "already-exported")
        self.assertEqual(recovered["commit"], first["commit"])
        self.assertEqual(
            git(self.destination, "rev-parse", "refs/seed/exports/" + revision).decode().strip(),
            first["commit"],
        )

        proposal = self.store.propose(revision, {"binary.bin": None, "next.txt": b"next\n"})
        second_revision = self.store.land(proposal)["revision"]
        second = export_seed_revision(self.store, self.destination, second_revision, branch="seed/pilot")
        self.assertEqual(second["status"], "exported")
        parents = git(self.destination, "rev-list", "--parents", "-n", "1", second["commit"]).decode().split()
        self.assertEqual(parents[1:], [first["commit"]])
        self.assertEqual(git(self.destination, "show", second["commit"] + ":next.txt"), b"next\n")
        self.assertEqual(git(self.destination, "show", second["commit"] + ":binary.bin", check=False), b"")

    def test_unsupported_git_modes_are_rejected_before_seed_changes(self):
        for mode, payload, path in (
            ("100755", b"#!/bin/sh\n", "run.sh"),
            ("120000", b"target", "link"),
            ("160000", self.commit.encode(), "submodule"),
        ):
            with self.subTest(mode=mode):
                source = self.root / ("mode-" + mode)
                initialize(source)
                if mode == "160000":
                    identifier = self.commit
                else:
                    identifier = git(source, "hash-object", "-w", "--stdin", input_data=payload).decode().strip()
                git(source, "read-tree", "--empty")
                git(source, "update-index", "--add", "--cacheinfo", f"{mode},{identifier},{path}")
                tree = git(source, "write-tree").decode().strip()
                commit = git(source, "commit-tree", tree, "-m", "unsupported").decode().strip()
                git(source, "update-ref", "refs/heads/main", commit)
                store = Store.create(self.root / ("unsupported-" + mode + ".db"))
                with self.assertRaisesRegex(SeedError, "regular 100644"):
                    import_git_commit(store, source, commit)
                self.assertEqual(store.log(), [{"seq": 0, "id": store.head(), "parent": None, "proposal": None}])

    def test_refuses_non_dedicated_or_unrelated_export_branch(self):
        revision = import_git_commit(self.store, self.source, self.commit)["revision"]
        with self.assertRaisesRegex(SeedError, "must start"):
            export_seed_revision(self.store, self.destination, revision, branch="main")
        git(self.destination, "fetch", "-q", str(self.source), "main:seed/foreign")
        user_tip = git(self.destination, "rev-parse", "seed/foreign").decode().strip()
        with self.assertRaisesRegex(SeedError, "not owned"):
            export_seed_revision(self.store, self.destination, revision, branch="seed/foreign")
        self.assertEqual(git(self.destination, "rev-parse", "seed/foreign").decode().strip(), user_tip)

    def test_git_environment_cannot_redirect_bridge_commands(self):
        foreign = self.root / "foreign"
        initialize(foreign)
        poisoned = {
            "GIT_DIR": str(foreign / ".git"),
            "GIT_WORK_TREE": str(foreign),
            "GIT_INDEX_FILE": str(foreign / "other.index"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(foreign / "hooks"),
        }
        with patch.dict(os.environ, poisoned, clear=False):
            imported = import_git_commit(self.store, self.source, self.commit)
            exported = export_seed_revision(self.store, self.destination, imported["revision"], branch="seed/isolated")
        self.assertEqual(exported["status"], "exported")
        self.assertEqual(git(foreign, "show-ref", check=False), b"")
        self.assertEqual(git(self.destination, "rev-parse", "seed/isolated").decode().strip(), exported["commit"])

    def test_symbolic_export_ref_never_updates_its_target(self):
        revision = import_git_commit(self.store, self.source, self.commit)["revision"]
        (self.destination / "main.txt").write_text("user main\n", encoding="utf-8")
        main = commit_all(self.destination, "user main")
        git(self.destination, "symbolic-ref", "refs/heads/seed/symbolic", "refs/heads/main")
        with self.assertRaisesRegex(SeedError, "symbolic"):
            export_seed_revision(self.store, self.destination, revision, branch="seed/symbolic")
        self.assertEqual(git(self.destination, "rev-parse", "refs/heads/main").decode().strip(), main)
        self.assertEqual(git(self.destination, "rev-parse", "refs/heads/seed/symbolic").decode().strip(), main)

    def test_bridge_does_not_run_reference_transaction_hooks(self):
        revision = import_git_commit(self.store, self.source, self.commit)["revision"]
        hook = self.destination / ".git" / "hooks" / "reference-transaction"
        marker = self.destination / ".git" / "bridge-hook-ran"
        hook.write_text(
            "#!/bin/sh\n"
            "echo invoked > \"$(git rev-parse --git-path bridge-hook-ran)\"\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
        exported = export_seed_revision(self.store, self.destination, revision, branch="seed/no-hooks")
        self.assertEqual(exported["status"], "exported")
        self.assertFalse(marker.exists())

    def test_export_refuses_branch_checked_out_in_linked_worktree(self):
        first_revision = import_git_commit(self.store, self.source, self.commit)["revision"]
        first = export_seed_revision(self.store, self.destination, first_revision, branch="seed/checked")
        linked = self.root / "linked-worktree"
        git(self.destination, "worktree", "add", "-q", str(linked), "seed/checked")
        try:
            proposal = self.store.propose(first_revision, {"next.txt": b"next\n"})
            second_revision = self.store.land(proposal)["revision"]
            with self.assertRaisesRegex(SeedError, "checked out"):
                export_seed_revision(self.store, self.destination, second_revision, branch="seed/checked")
            self.assertEqual(git(self.destination, "rev-parse", "seed/checked").decode().strip(), first["commit"])
        finally:
            git(self.destination, "worktree", "remove", "--force", str(linked), check=False)


if __name__ == "__main__":
    unittest.main()
