from __future__ import annotations

import base64
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from experiments.live_agent import run_study


POLICY = b'''def priority_weights(rules):
    key = "priorities" if rules.get("schema_version") == 1 else "priority_weights"
    return rules[key]
'''
REVIEWBOX = b'''import argparse, csv, json
from pathlib import Path
from catalog import STATUSES, load_assets, load_rules
from policy import priority_weights
ROOT = Path(__file__).resolve().parent
def assets(): return load_assets(ROOT / "data" / "assets.json")
def records(): return sorted(({k: row[k] for k in ("id", "name", "status", "kind")} for row in assets()), key=lambda row: row["id"])
def main(argv=None):
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest="command", required=True)
    listing=sub.add_parser("list"); listing.add_argument("--status", choices=sorted(STATUSES))
    export=sub.add_parser("export"); export.add_argument("--output", required=True)
    priority=sub.add_parser("priority"); priority.add_argument("--json", action="store_true")
    args=parser.parse_args(argv)
    if args.command == "list": print(json.dumps([row for row in records() if not args.status or row["status"] == args.status], ensure_ascii=False, sort_keys=True))
    elif args.command == "export":
        rows=assets(); fields=sorted({key for row in rows for key in row})
        with Path(args.output).open("w", encoding="utf-8", newline="") as stream:
            writer=csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    else:
        if not args.json: parser.error("--json is required")
        weights=priority_weights(load_rules(ROOT / "rules.json")); rows=[]
        for row in assets():
            if weights[row["status"]] > 0: rows.append(dict(row, weight=weights[row["status"]]))
        rows.sort(key=lambda row: (-row["weight"], row["id"])); counts={status: sum(row["status"] == status for row in assets()) for status in STATUSES}
        print(json.dumps({"items": rows, "counts": counts}, ensure_ascii=False, sort_keys=True))
if __name__ == "__main__": raise SystemExit(main())
'''


class LiveStudySmokeTests(unittest.TestCase):
    def test_git_environment_cannot_redirect_the_disposable_repository(self):
        with patch.dict(os.environ, {"GIT_DIR": "invalid", "git_work_tree": "invalid", "SEED_TOKEN": "private", "SEED_DATABASE_URL": "invalid"}):
            cleaned = run_study._clean_env()
        self.assertFalse(any(key.upper().startswith("GIT_") for key in cleaned))
        self.assertNotIn("SEED_TOKEN", cleaned)
        self.assertNotIn("SEED_DATABASE_URL", cleaned)

    def test_fake_adapter_exercises_all_cells_and_rework_without_a_model(self):
        parent = Path("artifacts/tmp").resolve()
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temporary:
            root = Path(temporary)
            adapter = root / "fake_adapter.py"
            adapter.write_text(textwrap.dedent(f'''\
                import argparse, base64, json
                from pathlib import Path
                parser=argparse.ArgumentParser(); parser.add_argument("--prompt-file"); parser.add_argument("--output-dir", type=Path); parser.add_argument("--app-dir", type=Path); parser.add_argument("--python"); parser.add_argument("--timeout"); parser.add_argument("--max-turns"); args=parser.parse_args()
                app=args.app_dir.resolve(); args.output_dir.mkdir(parents=True, exist_ok=False)
                (app / "policy.py").write_bytes(base64.b64decode({base64.b64encode(POLICY)!r}))
                (app / "reviewbox.py").write_bytes(base64.b64decode({base64.b64encode(REVIEWBOX)!r}))
                result={{"completed": True, "returncode": 0, "usage": None, "num_turns": 0}}
                (args.output_dir / "summary.json").write_text(json.dumps(result), encoding="utf-8")
                print(json.dumps(result))
            '''), encoding="utf-8")
            report = run_study.run(output=root / "out", python=sys.executable, adapter=adapter,
                                   timeout=120, max_turns=1)
        self.assertFalse(report["failures"])
        self.assertEqual(len(report["runs"]), 9)
        self.assertTrue(all(row["final"]["passed"] for row in report["runs"]))
        self.assertTrue(any(len(row["attempts"]) == 2 for row in report["runs"]))
        for row in report["runs"]:
            if row["arm"] == "seed" and row["card"] in {"docs-export", "config-priority"}:
                self.assertEqual(row["attempts"][0]["land"]["body"]["status"], "conflict")
                self.assertEqual(len(row["attempts"]), 2)


if __name__ == "__main__":
    unittest.main()
