"""Run a bounded live Claude coding study against Seed and two Git workflows.

The harness deliberately records individual observations.  Nine model calls are
not a statistical productivity claim: they are auditable end-to-end examples
with a common app, public evaluator, peer edit, and one bounded rework.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from typing import Any
from urllib.request import urlopen

from seed_client import Client

try:  # Supports both ``python -m`` and the documented direct script invocation.
    from experiments.live_agent import taskpack
except ModuleNotFoundError:  # pragma: no cover - exercised by the CLI itself
    import taskpack


ROOT = Path(__file__).resolve().parents[2]
ARMS = ("seed", "git", "conditional-git")
ORDER = {
    "clean-filter": ("seed", "git", "conditional-git"),
    "docs-export": ("git", "conditional-git", "seed"),
    "config-priority": ("conditional-git", "seed", "git"),
}


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _tree_hash(root: Path) -> str:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file() and ".git" not in item.parts):
        rows.append((path.relative_to(root).as_posix(), _sha(path.read_bytes())))
    return _sha(json.dumps(rows, separators=(",", ":")).encode())


def _write_files(root: Path, files: dict[str, bytes], *, prefix: str = "app") -> None:
    for name, payload in files.items():
        target = root / prefix / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None,
         timeout: float = 120, log: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("$ " + " ".join(command) + "\n\n[stdout]\n" + result.stdout + "\n[stderr]\n" + result.stderr,
                       encoding="utf-8")
    if check and result.returncode:
        raise RuntimeError("command failed (" + str(result.returncode) + "): " + " ".join(command)
                           + "\n" + result.stderr[-4000:])
    return result


def _clean_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith(("GIT_", "SEED_"))}


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false", *args],
                cwd=root, env=_clean_env(), check=check)


def _public_tests(app: Path, python: str, log: Path, *, card_id: str) -> dict[str, Any]:
    """Run only the public contract supplied to the coding agent."""
    env = _clean_env() | {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    began = time.monotonic()
    test = _run([python, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=app, env=env,
                timeout=60, log=log, check=False)
    probes: list[dict[str, Any]] = [{"name": "unittest", "returncode": test.returncode}]
    if card_id == "clean-filter":
        filtered = _run([python, "-B", "reviewbox.py", "list", "--status", "pending"], cwd=app, env=env,
                        timeout=20, log=log.with_name(log.stem + "-filter.log"), check=False)
        try:
            json.loads(filtered.stdout)
            parsed = True
        except json.JSONDecodeError:
            parsed = False
        probes.append({"name": "filter-json", "returncode": filtered.returncode, "json": parsed})
    elif card_id == "docs-export":
        exported = _run([python, "-B", "reviewbox.py", "export", "--output", str(log.with_suffix(".csv"))], cwd=app, env=env,
                        timeout=20, log=log.with_name(log.stem + "-export.log"), check=False)
        probes.append({"name": "export", "returncode": exported.returncode,
                       "nonempty": log.with_suffix(".csv").is_file() and log.with_suffix(".csv").stat().st_size > 0})
    elif card_id == "config-priority":
        priority = _run([python, "-B", "reviewbox.py", "priority", "--json"], cwd=app, env=env,
                        timeout=20, log=log.with_name(log.stem + "-priority.log"), check=False)
        try:
            json.loads(priority.stdout)
            parsed = True
        except json.JSONDecodeError:
            parsed = False
        probes.append({"name": "priority-json", "returncode": priority.returncode, "json": parsed})
    return {"passed": test.returncode == 0 and all(item["returncode"] == 0 and item.get("json", True)
                                                   and item.get("nonempty", True) for item in probes),
            "checks": probes, "elapsed_seconds": time.monotonic() - began,
            "diagnostics": (test.stdout + test.stderr)[-8000:] if test.returncode else ""}


def _seed_head_workspace(client: Client, revision: str, root: Path) -> Path:
    """Materialize the server's paged current head, never a candidate workspace."""
    app = root / "app"
    if app.exists():
        raise RuntimeError("head workspace already exists")
    cursor = ""
    files: dict[str, str] = {}
    while True:
        page = client.files(revision=revision, after=cursor, limit=1000)
        if page.get("revision") != revision or not isinstance(page.get("files"), dict):
            raise RuntimeError("Seed server returned an inconsistent paged head")
        files.update(page["files"])
        cursor = page.get("next")
        if cursor is None:
            break
    for logical, blob in files.items():
        if not logical.startswith("app/") or not isinstance(blob, str):
            raise RuntimeError("study head contains a non-app path")
        target = app / logical.removeprefix("app/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(client.read_blob(blob))
    return app


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_ready(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Seed server exited before ready")
        try:
            with urlopen(url + "/readyz", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    process.terminate()
    raise RuntimeError("Seed server did not become ready")


class SeedServer:
    def __init__(self, root: Path, python: str):
        self.root, self.python, self.process, self.tokens = root, python, None, {}
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "SeedServer":
        auth = self.root / "credentials.json"
        for card in taskpack.cards():
            repository = "study-" + card["id"]
            issued = _run([self.python, str(ROOT / "seed_server.py"), "token", "--auth-file", str(auth),
                           "--repo", repository, "--permissions", "read", "write", "create"], cwd=ROOT)
            token = issued.stdout.strip()
            if not token:
                raise RuntimeError("Seed token command returned an empty token")
            self.tokens[repository] = token
        env = _clean_env() | {"SEED_DATA": str(self.root / "data"), "SEED_AUTH_FILE": str(auth)}
        self.process = subprocess.Popen([self.python, str(ROOT / "seed_server.py"), "serve", "--port", str(self.port)],
                                        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        text=True)
        _wait_ready(self.url, self.process)
        return self

    def __exit__(self, *_: object) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def token_for(self, repository: str) -> str:
        try:
            return self.tokens[repository]
        except KeyError as exc:
            raise RuntimeError("no local credential for repository " + repository) from exc


def _adapter(python: str, adapter: Path, prompt: str, root: Path, logs: Path, *, timeout: int, max_turns: int) -> dict[str, Any]:
    if logs.exists():
        raise RuntimeError("adapter output already exists: " + str(logs))
    prompt_file = logs.parent / (logs.name + "-prompt.md")
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(prompt, encoding="utf-8")
    began = time.monotonic()
    result = _run([python, str(adapter), "--prompt-file", str(prompt_file), "--output-dir", str(logs),
                   "--timeout", str(timeout), "--max-turns", str(max_turns), "--python", python, "--app-dir", "app"],
                  cwd=root, env=_clean_env(), timeout=timeout + 120, log=logs / "adapter-invocation.log", check=False)
    try:
        telemetry: Any = json.loads(result.stdout)
    except json.JSONDecodeError:
        telemetry = {"stdout_parse_error": True}
    if result.returncode or not telemetry.get("completed"):
        raise RuntimeError("coding session did not complete; inspect its retained summary.json")
    return {"returncode": result.returncode, "wall_seconds": time.monotonic() - began, "telemetry": telemetry}


def _check_seed_agent(logs: Path) -> None:
    summary = logs / "summary.json"
    if not summary.is_file() or not json.loads(summary.read_text("utf-8")).get("completed"):
        raise RuntimeError("coding session did not complete; inspect its retained summary.json")


def _repair_prompt(card: dict[str, Any], decision: str, public: dict[str, Any] | None) -> str:
    diagnostics = ({key: public[key] for key in ("passed", "checks", "diagnostics")}
                   if public is not None else None)
    return (card["rework_prompt"] + "\n\nIntegration outcome: " + decision +
            ". Public tests/probes: " + json.dumps(diagnostics, ensure_ascii=False) +
            ". Inspect the current files and fix the requested feature. No further peer updates will occur.")


def _seed_call(python: str, server: SeedServer, repository: str, state: Path, *args: str, timeout: float = 120) -> dict[str, Any]:
    env = _clean_env() | {"SEED_TOKEN": server.token_for(repository)}
    result = _run([python, str(ROOT / "seed_agent.py"), "--url", server.url, "--repo", repository, "--state", str(state),
                   "--allow-http", *args], cwd=ROOT, env=env, timeout=timeout, check=False)
    try:
        body = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("seed-agent emitted invalid JSON") from exc
    if result.returncode not in (0, 1, 2):
        raise RuntimeError("seed-agent terminated unexpectedly")
    return {"returncode": result.returncode, "body": body}


def _seed_peer(client: Client, peer: dict[str, bytes]) -> str | None:
    if not peer:
        return None
    proposal = client.propose(client.head(), {"app/" + path: value for path, value in peer.items()}, author="peer", message="peer update")
    return client.land(proposal)["revision"]


def _seed_attempt(card: dict[str, Any], output: Path, server: SeedServer, *, python: str, adapter: Path,
                  timeout: int, max_turns: int) -> dict[str, Any]:
    repository = "study-" + card["id"]
    client = Client(server.url, repository, server.token_for(repository), allow_http=True)
    client.create()
    base = client.head()
    initial = client.propose(base, {"app/" + path: value for path, value in taskpack.base_files().items()}, author="setup", message="base")
    base_revision = client.land(initial)["revision"]
    state, workspace = output / "task.json", output / "workspace"
    _seed_call(python, server, repository, state, "begin")
    candidate_prompt = output / "candidate-prompt.md"
    candidate_prompt.write_text(card["prompt"], encoding="utf-8")
    run = _seed_call(python, server, repository, state, "run", "--workspace", str(workspace), "--prefix", "", "--command-timeout", str(timeout + 60), "--",
                     python, str(adapter), "--prompt-file", str(candidate_prompt), "--output-dir", str(output / "candidate"),
                     "--timeout", str(timeout), "--max-turns", str(max_turns), "--python", python, "--app-dir", "app", timeout=timeout + 120)
    _check_seed_agent(output / "candidate")
    candidate = taskpack.evaluate(workspace / "app", card["id"], "candidate", python=python, temporary_parent=output)
    peer_revision = _seed_peer(client, card["peer"])
    proposed = _seed_call(python, server, repository, state, "propose", "--author", "claude", "--message", card["id"])
    if proposed["returncode"] != 0 or "proposal" not in proposed["body"]:
        raise RuntimeError("Seed proposal failed: " + json.dumps(proposed["body"], ensure_ascii=False))
    landed = _seed_call(python, server, repository, state, "land")
    if landed["body"].get("status") not in {"landed", "conflict"}:
        raise RuntimeError("Seed landing failed: " + json.dumps(landed["body"], ensure_ascii=False))
    initial_head = client.head()
    public = (_public_tests(_seed_head_workspace(client, initial_head, output / "landed-candidate"), python, output / "candidate-public.log",
                            card_id=card["id"])
              if landed["body"].get("status") == "landed" else None)
    attempts = [{"kind": "candidate", "run": run, "evaluation": candidate, "proposal": proposed,
                 "land": landed, "public": public}]
    if landed["body"].get("status") != "landed" or not public["passed"]:
        rework_state, rework_workspace = output / "rework.json", output / "rework-workspace"
        _seed_call(python, server, repository, state, "rework", "--new-state", str(rework_state))
        rework_prompt = output / "rework-prompt.md"
        rework_prompt.write_text(_repair_prompt(card, landed["body"].get("status", "error"), public), encoding="utf-8")
        rework = _seed_call(python, server, repository, rework_state, "run", "--workspace", str(rework_workspace), "--prefix", "", "--command-timeout", str(timeout + 60), "--",
                            python, str(adapter), "--prompt-file", str(rework_prompt), "--output-dir", str(output / "rework"),
                            "--timeout", str(timeout), "--max-turns", str(max_turns), "--python", python, "--app-dir", "app", timeout=timeout + 120)
        _check_seed_agent(output / "rework")
        evaluation = taskpack.evaluate(rework_workspace / "app", card["id"], "final", python=python, temporary_parent=output)
        proposal = _seed_call(python, server, repository, rework_state, "propose", "--author", "claude", "--message", card["id"] + " rework")
        if proposal["returncode"] != 0 or "proposal" not in proposal["body"]:
            raise RuntimeError("Seed rework proposal failed: " + json.dumps(proposal["body"], ensure_ascii=False))
        final_land = _seed_call(python, server, repository, rework_state, "land")
        if final_land["body"].get("status") != "landed":
            raise RuntimeError("Seed rework landing failed: " + json.dumps(final_land["body"], ensure_ascii=False))
        rework_head = client.head()
        public = _public_tests(_seed_head_workspace(client, rework_head, output / "landed-rework"), python, output / "rework-public.log",
                               card_id=card["id"])
        attempts.append({"kind": "rework", "run": rework, "evaluation": evaluation, "proposal": proposal,
                         "land": final_land, "public": public})
        landed = final_land
    head = client.head()
    final_workspace = _seed_head_workspace(client, head, output / "final-head")
    final = taskpack.evaluate(final_workspace, card["id"], "final", python=python, temporary_parent=output)
    final["behavioral_passed"] = final["passed"]
    final["public_passed"] = public["passed"]
    final["passed"] = final["passed"] and public["passed"]
    return {"base_revision": base_revision, "peer_revision": peer_revision, "head": head, "attempts": attempts,
            "decision": landed["body"].get("status"), "final": final}


def _git_setup(root: Path, files: dict[str, bytes]) -> tuple[Path, Path, str]:
    repo, agent = root / "repo", root / "agent"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _git(repo, "config", "user.name", "Live study")
    _git(repo, "config", "user.email", "study@example.invalid")
    _git(repo, "config", "core.autocrlf", "false")
    _write_files(repo, files)
    _git(repo, "add", "app")
    _git(repo, "commit", "--quiet", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "worktree", "add", "--quiet", "-b", "agent", str(agent), base)
    return repo, agent, base


def _git_peer(repo: Path, peer: dict[str, bytes]) -> str | None:
    if not peer:
        return None
    _write_files(repo, peer)
    _git(repo, "add", "app")
    _git(repo, "commit", "--quiet", "-m", "peer update")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _git_manifest(repo: Path, revision: str) -> str:
    return _sha(_git(repo, "ls-tree", "-r", "-z", revision, "--", "app").stdout.encode())


def _git_integrate(repo: Path, agent: Path, base: str, arm: str) -> tuple[str, str, dict[str, Any]]:
    _git(agent, "add", "app")
    _git(agent, "commit", "--quiet", "--allow-empty", "-m", "agent candidate")
    guard = {"scope": "app", "base_manifest": _git_manifest(repo, base),
             "current_manifest": _git_manifest(repo, "HEAD")}
    guard["matches"] = guard["base_manifest"] == guard["current_manifest"]
    if arm == "conditional-git" and not guard["matches"]:
        return "conflict", "scope-changed", guard
    merge = _git(repo, "merge", "--no-edit", "--no-gpg-sign", "agent", check=False)
    if merge.returncode:
        _git(repo, "merge", "--abort", check=False)
        return "conflict", "merge-conflict", guard
    return "landed", "merged", guard


def _git_attempt(card: dict[str, Any], output: Path, arm: str, *, python: str, adapter: Path,
                 timeout: int, max_turns: int) -> dict[str, Any]:
    repo, agent, base = _git_setup(output, taskpack.base_files())
    first = _adapter(python, adapter, card["prompt"], agent, output / "candidate", timeout=timeout, max_turns=max_turns)
    candidate = taskpack.evaluate(agent / "app", card["id"], "candidate", python=python, temporary_parent=output)
    peer = _git_peer(repo, card["peer"])
    decision, reason, guard = _git_integrate(repo, agent, base, arm)
    public = (_public_tests(repo / "app", python, output / "candidate-public.log", card_id=card["id"])
              if decision == "landed" else None)
    final = taskpack.evaluate(repo / "app", card["id"], "final", python=python, temporary_parent=output) if decision == "landed" else None
    attempts = [{"kind": "candidate", "agent": first, "evaluation": candidate, "decision": decision, "reason": reason, "guard": guard,
                 "public": public, "final": final}]
    if decision != "landed" or not public["passed"]:
        retry = output / "rework"
        retry.mkdir()
        _git(repo, "worktree", "add", "--quiet", "-b", "rework", str(retry), "HEAD")
        second = _adapter(python, adapter, _repair_prompt(card, decision, public), retry, output / "rework-logs", timeout=timeout, max_turns=max_turns)
        _git(retry, "add", "app")
        _git(retry, "commit", "--quiet", "--allow-empty", "-m", "agent rework")
        merge = _git(repo, "merge", "--no-edit", "--no-gpg-sign", "rework", check=False)
        if merge.returncode:
            _git(repo, "merge", "--abort", check=False)
            raise RuntimeError("rework merge conflicted")
        public = _public_tests(repo / "app", python, output / "rework-public.log", card_id=card["id"])
        final = taskpack.evaluate(repo / "app", card["id"], "final", python=python, temporary_parent=output)
        attempts.append({"kind": "rework", "agent": second, "evaluation": final, "decision": "landed", "reason": "merged",
                         "public": public, "final": final})
        decision, reason = "landed", "reworked"
    final["behavioral_passed"] = final["passed"]
    final["public_passed"] = public["passed"]
    final["passed"] = final["passed"] and public["passed"]
    return {"base_revision": base, "peer_revision": peer, "head": _git(repo, "rev-parse", "HEAD").stdout.strip(),
            "attempts": attempts, "decision": decision, "reason": reason, "final": final}


def _freeze(adapter: Path, *, python: str, timeout: int, max_turns: int) -> dict[str, Any]:
    files = taskpack.base_files()
    cards = taskpack.cards()
    python_version = _run([python, "--version"], cwd=ROOT).stdout.strip()
    git_version = _run(["git", "--version"], cwd=ROOT, env=_clean_env()).stdout.strip()
    source_commit = _git(ROOT, "rev-parse", "HEAD").stdout.strip()
    return {"source_commit": source_commit,
            "base": {path: _sha(data) for path, data in sorted(files.items())},
            "cards": [{"id": card["id"], "prompt": _sha(card["prompt"].encode()), "rework_prompt": _sha(card["rework_prompt"].encode()),
                       "peer": {path: _sha(data) for path, data in sorted(card["peer"].items())}} for card in cards],
            "sources": {path.name: _sha(path.read_bytes()) for path in (Path(__file__), Path(taskpack.__file__), adapter)},
            "controller": {"python": str(Path(python).resolve()), "python_version": python_version,
                           "git_version": git_version, "timeout": timeout, "max_turns": max_turns,
                           "scope": "app", "rework_limit": 1}}


def _checkpoint(output: Path, result: dict[str, Any]) -> None:
    target = output / "result.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def run(*, output: Path, python: str, adapter: Path, timeout: int = 360, max_turns: int = 24, dry_run: bool = False) -> dict[str, Any]:
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("output must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    if not adapter.is_file():
        raise ValueError("adapter does not exist: " + str(adapter))
    frozen = _freeze(adapter, python=python, timeout=timeout, max_turns=max_turns)
    result: dict[str, Any] = {"format": "seed-live-agent-study/1", "frozen": frozen, "arms": list(ARMS),
                              "order": ORDER, "dry_run": dry_run, "runs": [], "failures": []}
    _checkpoint(output, result)
    if dry_run:
        result["note"] = "No model, server, or Git controller was started."
        _checkpoint(output, result)
        return result
    cards = {card["id"]: card for card in taskpack.cards()}
    with SeedServer(output / "seed-server", python) as server:
        for card_id, arms in ORDER.items():
            for arm in arms:
                attempt_root = output / "runs" / card_id / arm
                attempt_root.mkdir(parents=True)
                began = time.monotonic()
                print(f"[live-study] begin card={card_id} arm={arm}", file=sys.stderr, flush=True)
                try:
                    details = (_seed_attempt(cards[card_id], attempt_root, server, python=python, adapter=adapter, timeout=timeout, max_turns=max_turns)
                               if arm == "seed" else _git_attempt(cards[card_id], attempt_root, arm, python=python, adapter=adapter, timeout=timeout, max_turns=max_turns))
                    row = {"card": card_id, "arm": arm, "wall_seconds": time.monotonic() - began, **details}
                    result["runs"].append(row)
                    print(f"[live-study] end card={card_id} arm={arm} decision={row['decision']}", file=sys.stderr, flush=True)
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                    failure = {"card": card_id, "arm": arm, "wall_seconds": time.monotonic() - began,
                               "error": type(exc).__name__, "message": str(exc)}
                    result["failures"].append(failure)
                    print(f"[live-study] failed card={card_id} arm={arm}: {exc}", file=sys.stderr, flush=True)
                _checkpoint(output, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=360)
    parser.add_argument("--max-turns", type=int, default=24)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 3600 or not 1 <= args.max_turns <= 64:
        parser.error("timeout must be 1..3600 and max-turns 1..64")
    try:
        report = run(output=args.output, python=args.python, adapter=args.adapter.resolve(), timeout=args.timeout,
                     max_turns=args.max_turns, dry_run=args.dry_run)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _checkpoint(args.output, report)
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
