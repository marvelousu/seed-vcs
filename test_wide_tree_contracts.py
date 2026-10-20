"""Adversarial contracts for the conditional-Git wide-tree benchmark."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import wide_tree_benchmark as benchmark


class WideTreeGitContracts(unittest.TestCase):
    """Keep the comparison's Git durability and verification claims concrete."""

    def _directory(self):
        benchmark.TMP.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(prefix="wide-tree-contract-", dir=benchmark.TMP)

    def _published(self):
        temporary = self._directory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        prepared = benchmark.prepare(root / "prepared", width=8, layout="flat", proposals=4)
        target = root / "target"
        target.mkdir()
        decisions, _ = benchmark._git_publish(prepared, target, batch=4)
        repo = target / "git"
        head = benchmark._git(repo, "rev-parse", "refs/heads/main").stdout.strip().decode()
        return prepared, repo, decisions, head

    def test_missing_done_does_not_advance_main_or_publish_staging(self):
        """``feature done`` rejects an interrupted importer before ref publication."""
        with self._directory() as directory:
            root = Path(directory)
            repo = root / "repo"
            benchmark._git(root, "init", "--bare", "--object-format=sha1", str(repo))
            benchmark._fast_import(repo, b"".join((
                b"commit refs/heads/main\n",
                b"committer Test <test@example.invalid> 0 +0000\n",
                b"data 5\nbase\n",
                b"M 100644 inline base.txt\n",
                b"data 5\nbase\n\n",
            )))
            before = benchmark._git(repo, "rev-parse", "refs/heads/main").stdout.strip()
            interrupted = b"".join((
                b"feature done\n",
                b"commit refs/bench/staging\n",
                b"committer Test <test@example.invalid> 0 +0000\n",
                b"data 8\nstaging\n",
                b"from " + before + b"\n",
                b"M 100644 inline staged.txt\n",
                b"data 7\nstaged\n\n",
            ))
            command = [
                "git", "-c", f"core.hooksPath={os.devnull}",
                "-c", "commit.gpgsign=false", "-c", "core.fsync=all",
                "-c", "core.fsyncMethod=fsync", "-c", "fastimport.unpackLimit=1",
                "-c", "gc.auto=0", "fast-import", "--quiet",
            ]
            result = subprocess.run(
                command, cwd=repo, input=interrupted, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=benchmark._environment(), timeout=20,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(
                benchmark._git(repo, "rev-parse", "refs/heads/main").stdout.strip(), before
            )
            self.assertNotEqual(
                benchmark._git(
                    repo, "show-ref", "--verify", "--quiet", "refs/bench/staging", check=False
                ).returncode,
                0,
            )

    def test_pending_audit_and_payload_objects_survive_pruning(self):
        prepared, repo, _, _ = self._published()
        for proposal in prepared.proposals:
            self.assertEqual(
                benchmark._git(repo, "cat-file", "-p", proposal.git_audit_blob).stdout,
                proposal.audit,
            )
            self.assertEqual(
                benchmark._git(repo, "cat-file", "-p", proposal.git_blob).stdout,
                proposal.payload,
            )

    def test_git_verifier_rejects_unsafe_decision_and_wrong_head(self):
        prepared, repo, decisions, head = self._published()
        unsafe = [dict(item) for item in decisions]
        unsafe[0].update(accepted=True, status="landed")
        with self.assertRaises(AssertionError):
            benchmark._verify_git(prepared, repo, unsafe, head)
        with self.assertRaises(AssertionError):
            benchmark._verify_git(prepared, repo, decisions, prepared.git_background)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
