"""Create a small, reproducible public archive from live-agent study artifacts.

The raw study records intentionally stay private.  This module copies only frozen
sources, public outcomes, and selected telemetry summaries.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable


_DROP_KEYS = {
    "argv", "launchargv", "modelreport", "raw", "rawstdout",
    "rawstderr", "stderr", "stdout", "transcript",
}
_PUBLIC_SUMMARY_KEYS = {
    "decision", "decisions", "evaluation", "guard", "outcome", "outcomes",
    "publiccheck", "publicchecks", "reason", "reasons", "timing", "timings",
}
_EXCLUDED_PARTS = {".git", "__pycache__"}
_SECRET = re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|(?:anthropic|openai)_api_key\s*[:=]|ghp_[A-Za-z0-9]{12,})", re.I)


def _key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _roots(input_root: Path, output: Path, repo_root: Path | None) -> list[tuple[str, str]]:
    candidates = [(str(Path.home().resolve()), "<HOME>"), (str(input_root.resolve()), "<ARTIFACTS>"), (str(output.parent.resolve()), "<OUTPUT>")]
    if repo_root is not None:
        candidates.append((str(repo_root.resolve()), "<REPO>"))
    return sorted(((root.replace("/", "\\"), marker) for root, marker in candidates), key=lambda pair: len(pair[0]), reverse=True)


def _sanitize_text(value: str, replacements: Iterable[tuple[str, str]]) -> str:
    result = value
    for root, marker in replacements:
        for variant in {root, root.replace("\\", "/"), root.replace("\\", "\\\\")}:
            result = result.replace(variant, marker)
    return result


def _sanitize(value: Any, replacements: Iterable[tuple[str, str]]) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value, replacements)
    if isinstance(value, list):
        return [_sanitize(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: _sanitize(item, replacements)
            for key, item in value.items()
            if _key(str(key)) not in _DROP_KEYS
        }
    return value


def _public_fields(value: dict[str, Any], replacements: Iterable[tuple[str, str]]) -> dict[str, Any]:
    return {
        key: _sanitize(item, replacements)
        for key, item in value.items()
        if _key(str(key)) in _PUBLIC_SUMMARY_KEYS
    }


def _count(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, str)):
        return len(value)
    if isinstance(value, int):
        return value
    return None


def _metrics(summary: dict[str, Any], replacements: Iterable[tuple[str, str]]) -> dict[str, Any]:
    selected: dict[str, Any] = {
        "completed": summary.get("completed"),
        "models": summary.get("models"),
        "claude_version": summary.get("claude_version"),
        "wall_seconds": summary.get("wall_seconds"),
        "usage": summary.get("usage"),
        "model_usage": summary.get("model_usage"),
        "estimated_api_cost_usd": summary.get("estimated_api_cost_usd"),
        "tool_calls": summary.get("tool_calls"),
        "test_command_count": _count(summary.get("test_commands")),
        "num_turns": summary.get("num_turns"),
        "prompt_sha256": summary.get("prompt_sha256"),
        "returncode": summary.get("returncode"),
        "timed_out": summary.get("timed_out"),
        "resolved_model_matches": summary.get("resolved_model_matches"),
    }
    return {key: _sanitize(value, replacements) for key, value in selected.items()}


def _phase(path: Path) -> str | None:
    parts = {part.lower() for part in path.parts}
    if "candidate" in parts:
        return "candidate"
    if {"rework", "rework-logs", "rework-workspace", "final-head"} & parts:
        return "final"
    return None


def _context(summary: dict[str, Any], relative: Path) -> dict[str, Any]:
    parts = relative.parts
    lowered = [part.lower() for part in parts]
    result: dict[str, Any] = {"location": relative.as_posix(), "phase": _phase(relative), "arm": None, "card": None}
    for arm in ("seed", "git", "conditional-git"):
        if arm in lowered:
            result["arm"] = arm
            arm_index = lowered.index(arm)
            if arm_index > 0:
                result["card"] = parts[arm_index - 1]
            break
    if result["card"] is None and "runs" in lowered:
        runs_index = lowered.index("runs")
        if runs_index + 1 < len(parts):
            result["card"] = parts[runs_index + 1]
    return result


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _source_tree(app: Path, replacements: Iterable[tuple[str, str]]) -> dict[str, dict[str, str]]:
    files: dict[str, dict[str, str]] = {}
    for path in sorted(app.rglob("*")):
        if not path.is_file() or _EXCLUDED_PARTS & set(path.parts):
            continue
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        exported = _sanitize_text(text, replacements)
        files[path.relative_to(app).as_posix()] = {
            "text": exported,
            "original_sha256": hashlib.sha256(raw).hexdigest(),
            "export_sha256": hashlib.sha256(exported.encode("utf-8")).hexdigest(),
        }
    return files


def _source_records(input_root: Path, replacements: Iterable[tuple[str, str]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    layouts = (
        ("seed", "workspace/app", "candidate"),
        ("seed", "rework-workspace/app", "final"),
        ("seed", "final-head/app", "final"),
        ("git", "agent/app", "candidate"),
        ("git", "repo/app", "final"),
        ("git", "rework/app", "final"),
        ("conditional-git", "agent/app", "candidate"),
        ("conditional-git", "repo/app", "final"),
        ("conditional-git", "rework/app", "final"),
    )
    runs_root = input_root / "runs"
    if not runs_root.is_dir():
        return records
    for card_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        for arm, relative, phase in layouts:
            app = card_dir / arm / relative
            if app.is_dir():
                records.append({
                    "location": app.relative_to(input_root).as_posix(),
                    "card": card_dir.name,
                    "arm": arm,
                    "phase": phase,
                    "files": _source_tree(app, replacements),
                })
    return records


def _assert_public(value: Any, replacements: Iterable[tuple[str, str]]) -> None:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    forbidden = {
        variant
        for root, _marker in replacements
        for variant in (root, root.replace("\\", "/"), root.replace("\\", "\\\\"))
    }
    if any(item in rendered for item in forbidden):
        raise ValueError("unsanitized private path in export")
    if _SECRET.search(rendered):
        raise ValueError("credential-like value in export")


def build_export(input_root: Path, output: Path, *, repo_root: Path | None = None) -> dict[str, Any]:
    """Build and write the public archive; raw records are never copied."""
    input_root = input_root.resolve()
    output = output.resolve()
    result_path = input_root / "result.json"
    result = _read_json(result_path)
    replacements = _roots(input_root, output, repo_root)
    summaries: list[dict[str, Any]] = []
    for summary_path in sorted(input_root.rglob("summary.json")):
        summary = _read_json(summary_path)
        relative = summary_path.relative_to(input_root)
        record = _context(summary, relative)
        record["metrics"] = _metrics(summary, replacements)
        record["public"] = _public_fields(summary, replacements)
        summaries.append(record)
    archive = {
        "format": "live-agent-study-v2",
        "source_freeze": _sanitize(result.get("frozen"), replacements),
        "result": _sanitize(result, replacements),
        "summaries": summaries,
        "sources": _source_records(input_root, replacements),
    }
    _assert_public(archive, replacements)
    payload = json.dumps(archive, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as compressed:
            compressed.write(payload)
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path)
    args = parser.parse_args(argv)
    archive = build_export(args.input, args.output, repo_root=args.repo_root)
    print(json.dumps({"summaries": len(archive["summaries"]), "sources": len(archive["sources"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
