from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import os

from experiments.live_agent.taskpack import base_files, cards, evaluate


_REFERENCE_REVIEWBOX = '''import argparse
import csv
import json
from pathlib import Path

from catalog import STATUSES, load_assets, load_rules
from policy import priority_weights

ROOT = Path(__file__).resolve().parent

def assets():
    return load_assets(ROOT / "data" / "assets.json")

def records():
    return sorted(({key: item[key] for key in ("id", "name", "status", "kind")} for item in assets()), key=lambda item: item["id"])

def main(argv=None):
    parser = argparse.ArgumentParser(prog="reviewbox")
    commands = parser.add_subparsers(dest="command", required=True)
    listed = commands.add_parser("list")
    listed.add_argument("--status", choices=sorted(STATUSES))
    exported = commands.add_parser("export")
    exported.add_argument("--output", required=True)
    priority = commands.add_parser("priority")
    priority.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "list":
        result = records()
        if args.status:
            result = [row for row in result if row["status"] == args.status]
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    elif args.command == "export":
        fields = sorted({key for item in assets() for key in item})
        with Path(args.output).open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows(assets())
    elif args.command == "priority":
        if not args.json:
            parser.error("priority requires --json")
        weights = priority_weights(load_rules(ROOT / "rules.json"))
        result = [dict(item, weight=weights[item["status"]]) for item in assets() if weights[item["status"]] > 0]
        result.sort(key=lambda item: (-item["weight"], item["id"]))
        counts = {status: sum(item["status"] == status for item in assets()) for status in sorted(STATUSES)}
        print(json.dumps({"items": result, "counts": counts}, ensure_ascii=False, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''

_REFERENCE_POLICY = '''def priority_weights(rules):
    version = rules.get("schema_version")
    key = "priorities" if version == 1 else "priority_weights" if version == 2 else None
    weights = rules.get(key) if key else None
    if not isinstance(weights, dict) or any(not isinstance(value, int) for value in weights.values()):
        raise ValueError("invalid priorities")
    return weights
'''


class TaskpackContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="taskpack-", dir=Path.cwd())
        self.workspace = Path(self._temp.name) / "app"
        self.workspace.mkdir()
        for logical_path, contents in base_files().items():
            destination = self.workspace / logical_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(contents)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _install_reference_solution(self) -> None:
        (self.workspace / "reviewbox.py").write_text(_REFERENCE_REVIEWBOX, encoding="utf-8")
        (self.workspace / "policy.py").write_text(_REFERENCE_POLICY, encoding="utf-8")

    def test_base_files_are_a_runnable_sorted_json_application(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, "reviewbox.py", "list"], cwd=self.workspace, text=True,
            encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, env=environment,
        )
        rows = json.loads(result.stdout)
        self.assertEqual([row["id"] for row in rows], sorted(row["id"] for row in rows))
        self.assertEqual(set(rows[0]), {"id", "name", "status", "kind"})
        self.assertEqual({card["id"] for card in cards()}, {"clean-filter", "docs-export", "config-priority"})

    def test_evaluator_accepts_reference_solution_before_and_after_each_peer_update(self) -> None:
        self._install_reference_solution()
        for card in cards():
            with self.subTest(card=card["id"], phase="candidate"):
                candidate = evaluate(self.workspace, card["id"], "candidate", python=sys.executable, temporary_parent=Path.cwd())
                self.assertTrue(candidate["passed"], candidate["checks"])
            for logical_path, contents in card["peer"].items():
                (self.workspace / logical_path).write_bytes(contents)
            with self.subTest(card=card["id"], phase="final"):
                final = evaluate(self.workspace, card["id"], "final", python=sys.executable, temporary_parent=Path.cwd())
                self.assertTrue(final["passed"], final["checks"])
            for logical_path, contents in base_files().items():
                if logical_path in card["peer"]:
                    (self.workspace / logical_path).write_bytes(contents)

    def test_evaluator_reports_an_incomplete_candidate_without_aborting(self) -> None:
        result = evaluate(self.workspace, "clean-filter", "candidate", python=sys.executable, temporary_parent=Path.cwd())
        self.assertFalse(result["passed"])
        self.assertIn("filter-pending-valid-json", {check["name"] for check in result["checks"]})

    def test_evaluator_rejects_a_modified_protected_fixture(self) -> None:
        self._install_reference_solution()
        (self.workspace / "docs" / "usage.md").write_text("weakened\n", encoding="utf-8")
        result = evaluate(self.workspace, "clean-filter", "candidate", python=sys.executable, temporary_parent=Path.cwd())
        self.assertFalse(result["passed"])
        checks = {check["name"]: check["passed"] for check in result["checks"]}
        self.assertFalse(checks["protected-docs-usage.md"])


if __name__ == "__main__":
    unittest.main()
