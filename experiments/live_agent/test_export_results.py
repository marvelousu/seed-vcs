from __future__ import annotations

import gzip
import json
from pathlib import Path
import tempfile
import unittest

from experiments.live_agent.export_results import build_export


class ExportResultsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="export-results-", dir=Path.cwd())
        self.root = Path(self._temp.name) / "artifacts"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_nested_stdout_and_windows_paths_are_not_exported(self) -> None:
        self._write_json(self.root / "result.json", {
            "frozen": "abc123",
            "runs": [{"card": "clean-filter", "arm": "seed", "body": {"command": {"stdout": "C:\\Users\\maro6\\telemetry.json"}}}],
            "failures": [{"card": "clean-filter"}],
            "order": ["clean-filter"],
            "arms": ["seed", "git"],
            "raw_stdout": "C:\\Users\\maro6\\secret",
        })
        run = self.root / "runs" / "clean-filter" / "seed" / "candidate"
        self._write_json(run / "summary.json", {
            "completed": False,
            "models": ["claude-opus"],
            "claude_version": "1.2",
            "wall_seconds": 4.5,
            "usage": {"input_tokens": 12},
            "model_usage": {"claude-opus": {"input_tokens": 12}},
            "estimated_api_cost_usd": 0.01,
            "tool_counts": {"Read": 2},
            "tool_calls": {"Read": 2},
            "test_commands": ["python -m unittest"],
            "num_turns": 3,
            "returncode": 1,
            "timed_out": False,
            "resolved_model_matches": True,
            "prompt_sha256": "prompt",
            "evaluation": {"passed": False},
            "nested": {"command": {"stdout": "C:\\Users\\maro6\\telemetry.json"}},
        })
        app = self.root / "runs" / "clean-filter" / "seed" / "workspace" / "app"
        app.mkdir(parents=True)
        app.joinpath("reviewbox.py").write_text('PATH = "C:\\\\Users\\\\maro6\\\\work"\n', encoding="utf-8")
        app.joinpath("__pycache__").mkdir()
        app.joinpath("__pycache__", "ignored.pyc").write_bytes(b"ignored")
        for relative in ("seed/rework-workspace/app", "seed/final-head/app", "git/agent/app", "git/repo/app", "git/rework/app"):
            peer_app = self.root / "runs" / "clean-filter" / relative
            peer_app.mkdir(parents=True)
            peer_app.joinpath("reviewbox.py").write_text("print('ok')\n", encoding="utf-8")
        output = self.root.parent / "public.json.gz"

        archive = build_export(self.root, output, repo_root=Path.cwd())

        rendered = json.dumps(archive, ensure_ascii=False)
        self.assertNotIn("C:\\Users\\maro6", rendered)
        self.assertNotIn("telemetry.json", rendered)
        self.assertEqual(archive["source_freeze"], "abc123")
        self.assertEqual(archive["result"]["failures"], [{"card": "clean-filter"}])
        self.assertEqual(archive["result"]["arms"], ["seed", "git"])
        self.assertEqual(archive["summaries"][0]["metrics"]["test_command_count"], 1)
        self.assertEqual(archive["summaries"][0]["metrics"]["estimated_api_cost_usd"], 0.01)
        self.assertEqual(archive["summaries"][0]["metrics"]["tool_calls"], {"Read": 2})
        self.assertEqual({(source["arm"], source["phase"]) for source in archive["sources"]}, {("seed", "candidate"), ("seed", "final"), ("git", "candidate"), ("git", "final")})
        exported_file = archive["sources"][0]["files"]["reviewbox.py"]
        self.assertIn("<HOME>", exported_file["text"])
        self.assertNotEqual(exported_file["original_sha256"], exported_file["export_sha256"])
        with gzip.open(output, "rt", encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), archive)

    def test_gzip_output_is_reproducible(self) -> None:
        self._write_json(self.root / "result.json", {"frozen": "freeze", "runs": [], "failures": [], "order": [], "arms": []})
        destination_a = self.root.parent / "a.json.gz"
        destination_b = self.root.parent / "b.json.gz"
        build_export(self.root, destination_a, repo_root=Path.cwd())
        build_export(self.root, destination_b, repo_root=Path.cwd())
        self.assertEqual(destination_a.read_bytes(), destination_b.read_bytes())


if __name__ == "__main__":
    unittest.main()
