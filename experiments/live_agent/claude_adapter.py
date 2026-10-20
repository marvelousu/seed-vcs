"""Launch one fresh coding session; retain raw events outside its workspace.

This is an opt-in local experiment, never called by the normal test suite.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(temporary, path)


def summarize(path: Path) -> dict:
    result = None
    tools: dict[str, int] = {}
    test_commands = []
    models = set()
    for line in path.read_text("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get("message", {})
        if isinstance(message, dict):
            if message.get("model"):
                models.add(message["model"])
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name", "unknown")
                    tools[name] = tools.get(name, 0) + 1
                    command = block.get("input", {}).get("command", "")
                    if name == "Bash" and "unittest" in command:
                        test_commands.append(command)
        if event.get("type") == "result":
            result = event
    return {
        "completed": result is not None and not result.get("is_error", False),
        "result_subtype": result.get("subtype") if result else None,
        "model_report": result.get("result", "") if result else "",
        "models": sorted(models), "tool_calls": tools,
        "test_commands": test_commands,
        "usage": result.get("usage") if result else None,
        "model_usage": result.get("modelUsage") if result else None,
        "estimated_api_cost_usd": result.get("total_cost_usd") if result else None,
        "duration_api_ms": result.get("duration_api_ms") if result else None,
        "num_turns": result.get("num_turns") if result else None,
        "permission_denials": result.get("permission_denials") if result else None,
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--app-dir", type=Path, default=Path("."))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--claude", default=str(Path.home() / ".local/bin/claude.exe"))
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument("--max-turns", type=int, default=24)
    parser.add_argument("--timeout", type=float, default=360)
    args = parser.parse_args()
    root = args.app_dir.resolve(strict=True)
    output = args.output_dir.resolve()
    if output == root or root in output.parents or output in root.parents:
        parser.error("logs must be outside the model workspace")
    output.mkdir(parents=True, exist_ok=False)
    prompt = args.prompt_file.read_text("utf-8")
    python = str(Path(args.python).resolve()).replace("\\", "/")
    test_command = f'"{python}" -B -m unittest discover -s tests -v'
    prompt += ("\n\nUse the ordinary file tools to inspect and edit this application. "
               "Run this exact test command before finishing:\n" + test_command +
               "\nDo not use network access, install packages, use git, read parent directories, "
               "or modify supplied fixtures/docs/tests. You may add new test files. "
               "The working directory is the application root. Finish with a short implementation/test report.\n")
    (output / "prompt.txt").write_text(prompt, "utf-8")
    empty_mcp = output / "empty-mcp.json"
    empty_mcp.write_text('{"mcpServers":{}}', "utf-8")
    command = [args.claude, "-p", "--safe-mode", "--restricted",
               "--model", args.model, "--effort", "medium",
               "--strict-mcp-config", "--mcp-config", str(empty_mcp),
               "--tools", "Read,Write,Edit,Glob,Grep,Bash",
               "--allowedTools", f"Read,Write,Edit,Glob,Grep,Bash({test_command})",
               "--permission-mode", "dontAsk", "--permission-prompts", "none",
               "--settings", '{"disableAllHooks":true}',
               "--disable-slash-commands", "--no-chrome", "--no-session-persistence",
               "--max-turns", str(args.max_turns), "--output-format", "stream-json", "--verbose"]
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("SEED_", "GIT_", "ANTHROPIC_", "OPENAI_"))
                   and not any(word in key.upper() for word in ("TOKEN", "SECRET", "API_KEY"))}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUTF8"] = "1"
    (output / "tmp").mkdir()
    environment["TEMP"] = environment["TMP"] = str(output / "tmp")
    version = subprocess.run([args.claude, "--version"], env=environment, capture_output=True,
                             text=True, encoding="utf-8", timeout=30, check=True).stdout.strip()
    started = time.perf_counter()
    timed_out = False
    with (output / "events.jsonl").open("w", encoding="utf-8") as stdout, (output / "stderr.txt").open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(command, cwd=root, env=environment, stdin=subprocess.PIPE,
                                   stdout=stdout, stderr=stderr, text=True, encoding="utf-8")
        try:
            process.communicate(prompt, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=30)
            else:
                process.kill()
            process.wait(timeout=30)
    summary = summarize(output / "events.jsonl")
    summary.update({"returncode": process.returncode, "timed_out": timed_out,
                    "wall_seconds": time.perf_counter() - started,
                    "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    "requested_model": args.model, "effort": "medium",
                    "max_turns": args.max_turns, "claude_version": version,
                    "claude_executable": str(Path(args.claude).resolve()),
                    "launch_argv": command, "timeout_seconds": args.timeout,
                    "python_version": sys.version,
                    "resolved_model_matches": summary["models"] == [args.model]})
    summary["completed"] = bool(summary["completed"] and process.returncode == 0 and not timed_out)
    summary["completed"] = summary["completed"] and summary["resolved_model_matches"]
    atomic_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
