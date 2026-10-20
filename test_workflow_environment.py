"""An inherited Git environment must not redirect an isolated comparison."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import workflow_benchmark


class WorkflowEnvironmentTests(unittest.TestCase):
    def test_replay_does_not_write_an_inherited_repository(self):
        parent = Path(__file__).resolve().parent / "artifacts" / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            root = Path(directory)
            unrelated = root / "unrelated"
            unrelated.mkdir()
            env = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("GIT_")}
            subprocess.run(["git", "init", "-q", str(unrelated)], check=True, env=env)
            before = (unrelated / ".git" / "config").read_bytes()
            with patch.dict(os.environ, {"GIT_DIR": str(unrelated / ".git"),
                                         "GIT_WORK_TREE": str(unrelated),
                                         "GIT_INDEX_FILE": str(root / "unexpected-index")}):
                self.assertTrue(workflow_benchmark.git(root, workflow_benchmark.cases()[0]))
            self.assertEqual((unrelated / ".git" / "config").read_bytes(), before)
            self.assertFalse((unrelated / "src").exists())
            self.assertFalse((root / "unexpected-index").exists())


if __name__ == "__main__":
    unittest.main()
