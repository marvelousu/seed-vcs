from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent_comparison as comparison


def payload(value: str) -> dict[str, str]:
    return {"encoding": "utf-8", "data": value}


def fixture_trace(*, origin: str = "fixture") -> dict:
    return {
        "format": comparison.TRACE_FORMAT,
        "trace_id": "recorded-example",
        "recording": {"kind": origin, "label": "test only"},
        "workloads": [
            {
                "id": "independent", "group": "independent-edit",
                "initial": {"src/a.py": payload("old\n"), "docs/a.txt": payload("old\n")},
                "background": {"docs/a.txt": payload("new\n")},
                "agent": {
                    "actions": [{"op": "read", "path": "src/a.py"}],
                    "proposal": {"id": "p-independent", "edits": {"src/a.py": payload("candidate\n")}, "observed": {"reads": ["src/a.py"], "ranges": []}},
                    "rework": [], "model": {"input_tokens": None, "output_tokens": None, "cost_usd": None}, "transport": None,
                },
                "oracle": {"expected_safe": True, "reason": "The background edit is independent."},
            },
            {
                "id": "disjoint-lines", "group": "shared-file-disjoint-lines",
                "initial": {"src/shared.txt": payload("first\nsecond\nthird\n")},
                "background": {"src/shared.txt": payload("first\nsecond\nbackground\n")},
                "agent": {
                    "actions": [{"op": "read", "path": "src/shared.txt"}],
                    "proposal": {"id": "p-lines", "edits": {"src/shared.txt": payload("candidate\nsecond\nthird\n")}, "observed": {"reads": ["src/shared.txt"], "ranges": []}},
                    "rework": [{"reason": "recorded only"}], "model": None, "transport": None,
                },
                "oracle": {"expected_safe": True, "reason": "The lines changed by the two tasks do not overlap."},
            },
            {
                "id": "changed-read", "group": "changed-read-input",
                "initial": {"price.txt": payload("700\n")},
                "background": {"price.txt": payload("800\n")},
                "agent": {
                    "actions": [{"op": "read", "path": "price.txt"}],
                    "proposal": {"id": "p-price", "edits": {"total.txt": payload("770\n")}, "observed": {"reads": ["price.txt"], "ranges": []}},
                    "rework": [], "model": {"input_tokens": 120, "output_tokens": 30, "cost_usd": 0.01}, "transport": None,
                },
                "oracle": {"expected_safe": False, "reason": "The output depends on the changed input price."},
            },
        ],
    }


class ComparisonTests(unittest.TestCase):
    def test_fixture_origin_requires_opt_in_and_groups_are_strict(self):
        trace = fixture_trace()
        with self.assertRaises(comparison.TraceError):
            comparison.parse_trace(trace)
        workloads = comparison.parse_trace(trace, allow_fixture=True)
        self.assertEqual([workload.group for workload in workloads], list(comparison.GROUPS))
        trace["workloads"][2]["group"] = "independent-edit"
        with self.assertRaises(comparison.TraceError):
            comparison.parse_trace(trace, allow_fixture=True)

    def test_observed_dependencies_and_oracle_remain_separate(self):
        workloads = comparison.parse_trace(fixture_trace(), allow_fixture=True)
        independent, _, changed = workloads
        self.assertTrue(comparison.observations_match(independent))
        self.assertFalse(comparison.observations_match(changed))
        self.assertFalse(changed.expected_safe)
        self.assertEqual(changed.oracle_reason, "The output depends on the changed input price.")

    def test_paired_run_records_each_backend_and_oracle_outcomes(self):
        workloads = comparison.parse_trace(fixture_trace(), allow_fixture=True)

        def seed(_, workload):
            return comparison.observations_match(workload), {"setup_seconds": 0.1, "proposal_seconds": 0.1, "background_seconds": 0.1, "integration_seconds": 0.1}, {"requests": None, "upload_bytes": None, "download_bytes": None, "request_seconds": None}

        def git(_, workload, *, conditional, command_timeout):
            accepted = comparison.observations_match(workload) if conditional else True
            return accepted, {"setup_seconds": 0.1, "proposal_seconds": 0.1, "background_seconds": 0.1, "integration_seconds": 0.1}, {"requests": 0, "upload_bytes": 0, "download_bytes": 0, "request_seconds": 0.0}

        with tempfile.TemporaryDirectory(dir=Path("artifacts/tmp").resolve()) as temporary:
            with patch.object(comparison, "_seed_replay", side_effect=seed), patch.object(comparison, "_git_replay", side_effect=git):
                result = comparison.run(workloads, repeats=1, temp_root=Path(temporary))
        self.assertFalse(result["live_agent_productivity_measured"])
        self.assertEqual(len(result["results"]), 9)
        self.assertEqual(result["summary"]["git-worktree"]["unsafe_accepts"], 1)
        self.assertEqual(result["summary"]["seed"]["unsafe_accepts"], 0)
        self.assertEqual(result["summary"]["seed"]["conservative_rejections"], 1)

    def test_cli_writes_fixture_replay_only_with_explicit_opt_in(self):
        parent = Path("artifacts/tmp").resolve()
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temporary:
            root = Path(temporary)
            trace_path, output = root / "trace.json", root / "result.json"
            trace_path.write_text(json.dumps(fixture_trace()), encoding="utf-8")
            with patch.object(comparison, "run", return_value={"kind": "recorded-agent-trace-paired-replay"}):
                self.assertEqual(comparison.main(["replay", "--trace", str(trace_path), "--repeats", "5", "--output", str(output), "--allow-fixture"]), 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["kind"], "recorded-agent-trace-paired-replay")

    def test_capture_merges_only_adapter_agent_fields_with_fixed_oracle(self):
        parent = Path("artifacts/tmp").resolve()
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temporary:
            root = Path(temporary)
            spec, output = root / "spec.json", root / "trace.json"
            envelope = fixture_trace()
            adapter_workloads = []
            for workload in envelope["workloads"]:
                agent = workload.pop("agent")
                adapter_workloads.append({"id": workload["id"], **agent})
            spec.write_text(json.dumps(envelope), encoding="utf-8")
            script = "import json; print(json.dumps(" + repr({"format": comparison.ADAPTER_FORMAT, "workloads": adapter_workloads}) + "))"
            result = comparison._capture(spec, output, [sys.executable, "-c", script], 10)
            self.assertEqual(result["workloads"], 3)
            captured = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(captured["workloads"][2]["oracle"]["expected_safe"], False)
            self.assertEqual(captured["workloads"][0]["agent"]["proposal"]["id"], "p-independent")

    def test_git_calls_clear_inherited_repository_routing_and_disable_hooks(self):
        completed = __import__("subprocess").CompletedProcess(["git"], 0, "", "")
        with patch.dict("os.environ", {"GIT_DIR": "outside", "GIT_WORK_TREE": "elsewhere", "GIT_INDEX_FILE": "index"}, clear=False):
            with patch.object(comparison.subprocess, "run", return_value=completed) as invoke:
                comparison._git_call(Path("safe-root"), 10, "status")
        command = invoke.call_args.args[0]
        environment = invoke.call_args.kwargs["env"]
        self.assertEqual(command[:5], ["git", "-c", f"core.hooksPath={comparison.os.devnull}", "-c", "commit.gpgsign=false"])
        self.assertEqual(command[-1], "status")
        self.assertFalse(any(key.upper().startswith("GIT_") for key in environment))


if __name__ == "__main__":
    unittest.main()
