"""Telemetry tests use fixture events; they never invoke a model."""
import json
from pathlib import Path
import tempfile
import unittest

from experiments.live_agent.claude_adapter import summarize


class Telemetry(unittest.TestCase):
    def test_categories_and_missing_metrics_are_preserved(self):
        events = [{"type": "assistant", "message": {"model": "model-a", "content": [
            {"type": "tool_use", "name": "Read", "input": {}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "python -m unittest"}}]}},
            {"type": "result", "subtype": "success", "is_error": False,
             "usage": {"input_tokens": 7, "cache_read_input_tokens": 80, "output_tokens": 9}}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("not-json\n" + "\n".join(json.dumps(e) for e in events), "utf-8")
            result = summarize(path)
        self.assertTrue(result["completed"])
        self.assertEqual(result["tool_calls"], {"Read": 1, "Bash": 1})
        self.assertEqual(result["test_commands"], ["python -m unittest"])
        self.assertEqual(result["usage"]["cache_read_input_tokens"], 80)
        self.assertIsNone(result["estimated_api_cost_usd"])

    def test_error_or_absent_result_cannot_claim_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("", "utf-8")
            self.assertFalse(summarize(path)["completed"])
            path.write_text(json.dumps({"type": "result", "is_error": True, "result": "rate limit"}), "utf-8")
            self.assertFalse(summarize(path)["completed"])


if __name__ == "__main__":
    unittest.main()
