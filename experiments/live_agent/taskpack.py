"""Frozen ReviewBox task fixtures and backend-neutral black-box evaluation."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any


_ASSETS = [
    {"id": "asset-04", "name": "星の設計図", "status": "revise", "kind": "image", "comment": "review, \"needs contrast\""},
    {"id": "asset-01", "name": "夜空のタイトル画面", "status": "pending", "kind": "image", "comment": "title screen"},
    {"id": "asset-05", "name": "menu.csv", "status": "skip", "kind": "data", "comment": "duplicate"},
    {"id": "asset-02", "name": "Launch copy", "status": "keep", "kind": "text", "comment": "ready"},
    {"id": "asset-03", "name": "宇宙船のSE", "status": "pending", "kind": "audio", "comment": "短い確認メモ"},
]
_RULES_V1 = {"schema_version": 1, "priorities": {"pending": 1, "revise": 2, "keep": 0, "skip": 0}}
_RULES_V2 = {"schema_version": 2, "priority_weights": {"pending": 2, "revise": 5, "keep": 0, "skip": 0}}
_STATUSES = ("pending", "keep", "revise", "skip")
_PROTECTED_BASELINES = ("data/assets.json", "rules.json", "docs/usage.md", "tests/test_base.py")


def _json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def base_files() -> dict[str, bytes]:
    """Return the immutable logical files for one app-root materialization."""
    return {
        "catalog.py": b'''"""ReviewBox data loading."""
import json
from pathlib import Path

STATUSES = {"pending", "keep", "revise", "skip"}

def load_assets(path: Path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("assets must be a JSON list")
    for item in value:
        if not isinstance(item, dict) or set(("id", "name", "status", "kind")) - set(item):
            raise ValueError("each asset needs id, name, status, and kind")
        if item["status"] not in STATUSES:
            raise ValueError("unknown asset status")
    return value

def load_rules(path: Path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("rules must be a JSON object")
    return value
''',
        "policy.py": b'''"""ReviewBox policy helpers."""

def priority_weights(rules):
    if rules.get("schema_version") != 1:
        raise ValueError("unsupported rules schema")
    weights = rules.get("priorities")
    if not isinstance(weights, dict) or any(not isinstance(value, int) for value in weights.values()):
        raise ValueError("invalid priorities")
    return weights
''',
        "reviewbox.py": b'''"""A small local asset-review command line tool."""
import argparse
import json
from pathlib import Path

from catalog import STATUSES, load_assets

ROOT = Path(__file__).resolve().parent

def records():
    return sorted(({"id": item["id"], "name": item["name"], "status": item["status"], "kind": item["kind"]}
                   for item in load_assets(ROOT / "data" / "assets.json")), key=lambda item: item["id"])

def main(argv=None):
    parser = argparse.ArgumentParser(prog="reviewbox")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="print review records as JSON")
    args = parser.parse_args(argv)
    if args.command == "list":
        print(json.dumps(records(), ensure_ascii=False, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
''',
        "data/assets.json": _json(_ASSETS),
        "rules.json": _json(_RULES_V1),
        "docs/usage.md": b"# ReviewBox\n\n`python reviewbox.py list` prints review records as JSON sorted by ID.\n",
        "tests/test_base.py": b'''import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]

class BaseBehavior(unittest.TestCase):
    def test_list_is_json_sorted_by_id(self):
        result = subprocess.run([sys.executable, "reviewbox.py", "list"], cwd=ROOT, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        rows = json.loads(result.stdout)
        self.assertEqual([row["id"] for row in rows], sorted(row["id"] for row in rows))
        self.assertEqual(set(rows[0]), {"id", "name", "status", "kind"})

if __name__ == "__main__":
    unittest.main()
''',
    }


def cards() -> list[dict[str, Any]]:
    """Return independent feature cards; the peer patch is applied only at rework."""
    common = (
        "Use the ordinary available Read, Write, Edit, Glob, Grep, and Bash tools to inspect, edit, and test ReviewBox. "
        "Implement the requested feature and focused new tests, using the Python executable supplied by the harness. "
        "Only edit implementation files and add tests. Preserve the provided fixtures, tests, and documentation unless this card explicitly asks for a documentation change. "
        "Do not change status meanings or hard-code fixture-specific output."
    )
    filter_prompt = common + (
        "\n\nAdd `reviewbox.py list --status STATUS`. It must validate STATUS, return the same JSON "
        "record shape as list, remain sorted by id, and leave data/assets.json unchanged."
    )
    export_prompt = common + (
        "\n\nAdd `reviewbox.py export --output PATH`. Write every field of every asset as UTF-8 CSV, "
        "with an alphabetically sorted header containing the union of all field names and rows in input order. "
        "Use proper CSV escaping, preserve all values, and fail cleanly when PATH cannot be opened. "
        "Do not create missing parent directories. Do not modify docs for this card."
    )
    priority_prompt = common + (
        "\n\nAdd `reviewbox.py priority --json`. Load rules.json at runtime through policy.py, print a JSON "
        "object containing `items` and `counts`, include only positive-weight statuses in items, and retain every "
        "asset field plus a numeric `weight` property, with no additional fields in each item. Sort items by descending numeric weight then id. "
        "`counts` must count every known status. `--json` is mandatory for this command."
    )
    return [
        {"id": "clean-filter", "prompt": filter_prompt, "peer": {}, "rework_prompt": filter_prompt},
        {
            "id": "docs-export", "prompt": export_prompt,
            "peer": {"docs/usage.md": b"# ReviewBox\n\nThe review team now records provenance in the usage guide.\n"},
            "rework_prompt": export_prompt + "\n\nA teammate updated docs/usage.md while you worked. Rework from the current project state; preserve that documentation update.",
        },
        {
            "id": "config-priority", "prompt": priority_prompt,
            "peer": {
                "rules.json": _json(_RULES_V2),
                "docs/usage.md": b"# ReviewBox\n\nRules schema version 2 stores numeric `priority_weights` under that exact key.\n",
            },
            "rework_prompt": priority_prompt + "\n\nA teammate migrated rules.json to schema version 2. Rework from the current state. The feature must use the current schema at runtime, not retain the old key or old weights.",
        },
    ]


def _card(card_id: str) -> dict[str, Any]:
    for card in cards():
        if card["id"] == card_id:
            return card
    raise ValueError(f"Unknown task card: {card_id}")


def _clean_environment(temp: Path) -> dict[str, str]:
    names = ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TMP", "TEMP")
    result = {name: os.environ[name] for name in names if name in os.environ}
    result.update({"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(temp)})
    return result


def _run(python: str, workspace: Path, temp: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [python, str(workspace / "reviewbox.py"), *arguments], cwd=temp, text=True,
        encoding="utf-8", errors="strict", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=_clean_environment(temp), timeout=20, check=False,
    )


def _check(checks: list[dict[str, Any]], name: str, result: subprocess.CompletedProcess[str], passed: bool) -> None:
    checks.append({"name": name, "passed": bool(passed), "stdout": result.stdout, "stderr": result.stderr})


def _failed_check(checks: list[dict[str, Any]], name: str, result: subprocess.CompletedProcess[str], message: str) -> None:
    checks.append({"name": name, "passed": False, "stdout": result.stdout, "stderr": f"{result.stderr}\n{message}"})


def _load_json(checks: list[dict[str, Any]], name: str, result: subprocess.CompletedProcess[str]) -> Any | None:
    if result.returncode != 0:
        _failed_check(checks, name, result, f"command exited {result.returncode}")
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        _failed_check(checks, name, result, f"invalid JSON: {error}")
        return None


def evaluate(
    workspace: Path, card_id: str, phase: str = "final", *, python: str, temporary_parent: Path | None = None,
) -> dict[str, Any]:
    """Run frozen black-box checks. Setup errors intentionally propagate."""
    if phase not in {"candidate", "final"}:
        raise ValueError("phase must be candidate or final")
    card = _card(card_id)
    workspace = workspace.resolve()
    parent = (temporary_parent or Path(tempfile.gettempdir())).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    began = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="reviewbox-eval-", dir=parent) as directory:
        temp = Path(directory)
        checks: list[dict[str, Any]] = []
        assets_path = workspace / "data" / "assets.json"
        before = assets_path.read_bytes() if assets_path.is_file() else None
        listed = _run(python, workspace, temp, "list")
        rows = _load_json(checks, "base-list-valid-json", listed)
        expected_rows = [{key: item[key] for key in ("id", "name", "status", "kind")}
                         for item in sorted(_ASSETS, key=lambda item: item["id"])]
        _check(checks, "base-list-exact-records", listed, rows == expected_rows)
        _check(checks, "base-list-nonmutation", listed, before is not None and assets_path.is_file() and assets_path.read_bytes() == before)

        if card_id == "clean-filter":
            filtered = _run(python, workspace, temp, "list", "--status", "pending")
            filtered_rows = _load_json(checks, "filter-pending-valid-json", filtered)
            _check(checks, "filter-pending", filtered, filtered_rows == [row for row in expected_rows if row["status"] == "pending"])
            revised = _run(python, workspace, temp, "list", "--status", "revise")
            revised_rows = _load_json(checks, "filter-revise-valid-json", revised)
            _check(checks, "filter-revise", revised, revised_rows == [row for row in expected_rows if row["status"] == "revise"])
            empty = _run(python, workspace, temp, "list", "--status", "keep")
            empty_rows = _load_json(checks, "filter-keep-valid-json", empty)
            _check(checks, "filter-keep", empty, empty_rows == [row for row in expected_rows if row["status"] == "keep"])
            skipped = _run(python, workspace, temp, "list", "--status", "skip")
            skipped_rows = _load_json(checks, "filter-skip-valid-json", skipped)
            _check(checks, "filter-skip", skipped, skipped_rows == [row for row in expected_rows if row["status"] == "skip"])
            invalid = _run(python, workspace, temp, "list", "--status", "unknown")
            _check(checks, "filter-invalid-status", invalid, invalid.returncode != 0)
            missing = _run(python, workspace, temp, "list", "--status")
            _check(checks, "filter-missing-status", missing, missing.returncode != 0)
        elif card_id == "docs-export":
            output = temp / "assets.csv"
            exported = _run(python, workspace, temp, "export", "--output", str(output))
            headers: list[str] | None = None
            parsed: list[dict[str, str]] | None = None
            if exported.returncode == 0:
                try:
                    with output.open("r", encoding="utf-8", newline="") as stream:
                        parsed = list(csv.DictReader(stream))
                        stream.seek(0)
                        headers = next(csv.reader(stream))
                except (OSError, UnicodeError, csv.Error, StopIteration) as error:
                    _failed_check(checks, "export-readable-csv", exported, str(error))
            expected_headers = sorted({key for item in _ASSETS for key in item})
            _check(checks, "export-stable-header", exported, exported.returncode == 0 and headers == expected_headers)
            _check(checks, "export-unicode-and-escaping", exported,
                   parsed == [{key: item.get(key, "") for key in expected_headers} for item in _ASSETS])
            bad = _run(python, workspace, temp, "export", "--output", str(temp))
            _check(checks, "export-output-error", bad, bad.returncode != 0)
            missing_parent = _run(python, workspace, temp, "export", "--output", str(temp / "missing" / "assets.csv"))
            _check(checks, "export-missing-parent-error", missing_parent, missing_parent.returncode != 0)
        else:
            rules = _RULES_V1 if phase == "candidate" else _RULES_V2
            expected_schema = 1 if phase == "candidate" else 2
            rules_path = workspace / "rules.json"
            try:
                actual_rules = json.loads(rules_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                actual_rules = None
                _failed_check(checks, "priority-readable-rules", listed, str(error))
            probe = _run(python, workspace, temp, "priority", "--json")
            output = _load_json(checks, "priority-valid-json", probe)
            weights = rules["priorities"] if expected_schema == 1 else rules["priority_weights"]
            items = [dict(item, weight=weights[item["status"]]) for item in _ASSETS if weights[item["status"]] > 0]
            items.sort(key=lambda item: (-item["weight"], item["id"]))
            counts = {status: sum(item["status"] == status for item in _ASSETS) for status in _STATUSES}
            _check(checks, "priority-current-schema", probe, actual_rules == rules)
            _check(checks, "priority-weighted-order", probe, isinstance(output, dict) and output.get("items") == items)
            _check(checks, "priority-all-status-counts", probe, isinstance(output, dict) and output.get("counts") == counts)
            no_flag = _run(python, workspace, temp, "priority")
            _check(checks, "priority-requires-json", no_flag, no_flag.returncode != 0)

        if phase == "final":
            peer_current = all((workspace / path).is_file() and (workspace / path).read_bytes() == contents for path, contents in card["peer"].items())
            _check(checks, "peer-update-preserved", listed, peer_current)
        expected_protected = base_files()
        if phase == "final":
            expected_protected.update(card["peer"])
        for logical_path in _PROTECTED_BASELINES:
            protected_path = workspace / logical_path
            _check(
                checks,
                f"protected-{logical_path.replace('/', '-')}",
                listed,
                protected_path.is_file() and protected_path.read_bytes() == expected_protected[logical_path],
            )
        _check(checks, "feature-nonmutation", listed, before is not None and assets_path.is_file() and assets_path.read_bytes() == before)
        return {"passed": all(check["passed"] for check in checks), "checks": checks,
                "elapsed_seconds": time.monotonic() - began}
