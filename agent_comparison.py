"""Paired replay of recorded agent traces against Seed and Git worktrees.

This harness compares integration decisions for the same *recorded* candidate
edits.  It deliberately does not claim to measure model productivity: model
costs and token counts are trace metadata, and are null when a recorder did not
provide them.  The independent safety oracle is never inferred from a trace's
observed reads.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any, Iterable

from seed import Conflict, SeedError, Store, file_path
from workflow_benchmark import fingerprint as _workflow_fingerprint


TRACE_FORMAT = "seed-agent-comparison-trace/1"
ADAPTER_FORMAT = "seed-agent-comparison-adapter-result/1"
GROUPS = ("independent-edit", "shared-file-disjoint-lines", "changed-read-input")
BACKENDS = ("seed", "git-worktree", "git-conditional-worktree")
MAX_TRACE_BYTES = 4 * 1024 * 1024
MAX_FILES = 1_000
MAX_FILE_BYTES = 1 * 1024 * 1024
_WINDOWS_RESERVED_BASENAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class TraceError(ValueError):
    """A trace is malformed or cannot support a fair paired replay."""


@dataclass(frozen=True)
class Workload:
    trace_id: str
    workload_id: str
    group: str
    initial: dict[str, bytes]
    background: dict[str, bytes | None]
    edits: dict[str, bytes | None]
    reads: tuple[str, ...]
    ranges: tuple[tuple[str, str], ...]
    actions: tuple[dict[str, Any], ...]
    proposal_id: str | None
    rework_events: tuple[dict[str, Any], ...]
    expected_safe: bool
    oracle_reason: str
    model: dict[str, int | float | None]
    recorded_transport: dict[str, int | float | None]
    origin: str


def _decode_payload(value: Any, field: str) -> bytes:
    if not isinstance(value, dict) or set(value) != {"encoding", "data"}:
        raise TraceError(f"{field} must be an encoded payload object")
    encoding, data = value["encoding"], value["data"]
    if not isinstance(data, str):
        raise TraceError(f"{field}.data must be a string")
    if encoding == "utf-8":
        return data.encode("utf-8")
    if encoding == "base64":
        try:
            return base64.b64decode(data.encode("ascii"), validate=True)
        except (UnicodeError, ValueError) as exc:
            raise TraceError(f"{field} has invalid base64") from exc
    raise TraceError(f"{field}.encoding must be utf-8 or base64")


def _encode_payload(payload: bytes) -> dict[str, str]:
    try:
        return {"encoding": "utf-8", "data": payload.decode("utf-8")}
    except UnicodeDecodeError:
        return {"encoding": "base64", "data": base64.b64encode(payload).decode("ascii")}


def _portable_path(value: str) -> str:
    """Validate a Seed path that will also be materialized on Windows.

    Seed's logical namespace intentionally permits some names which cannot be
    represented safely and consistently by a local worktree (for example
    ``name:stream``).  Comparison traces are portable worktree input, so their
    stricter namespace is deliberate.
    """
    try:
        path = file_path(value)
    except SeedError as exc:
        raise TraceError(f"Not a portable trace path: {value!r}") from exc
    for part in path.split("/"):
        if (part.endswith((" ", "."))
                or any(ord(character) < 32 or character in '<>:"\\|?*' for character in part)
                or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_BASENAMES):
            raise TraceError(f"Not a portable trace path: {value!r}")
    return path


def _paths(value: Any, field: str, *, allow_delete: bool) -> dict[str, bytes | None]:
    if not isinstance(value, dict) or len(value) > MAX_FILES:
        raise TraceError(f"{field} must be an object with at most {MAX_FILES} paths")
    result: dict[str, bytes | None] = {}
    for path, encoded in value.items():
        if not isinstance(path, str):
            raise TraceError(f"{field} paths must be strings")
        path = _portable_path(path)
        if encoded is None and allow_delete:
            result[path] = None
            continue
        if encoded is None:
            raise TraceError(f"{field}.{path} cannot be null")
        payload = _decode_payload(encoded, f"{field}.{path}")
        if len(payload) > MAX_FILE_BYTES:
            raise TraceError(f"{field}.{path} exceeds {MAX_FILE_BYTES} bytes")
        result[path] = payload
    return result


def _ranges(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or len(value) > 64:
        raise TraceError("observed.ranges must be a list with at most 64 descriptors")
    result: set[tuple[str, str]] = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"kind", "prefix"}:
            raise TraceError("Each observed range must have kind and prefix")
        kind, prefix = entry["kind"], entry["prefix"]
        if kind not in {"membership", "subtree"} or not isinstance(prefix, str):
            raise TraceError("Invalid observed range")
        prefix = "" if not prefix else _portable_path(prefix.rstrip("/")) + "/"
        result.add((kind, prefix))
    return tuple(sorted(result))


def _model(value: Any) -> dict[str, int | float | None]:
    if value is None:
        return {"input_tokens": None, "output_tokens": None, "cost_usd": None}
    if not isinstance(value, dict) or set(value) - {"input_tokens", "output_tokens", "cost_usd"}:
        raise TraceError("agent.model has unsupported fields")
    result: dict[str, int | float | None] = {}
    for key in ("input_tokens", "output_tokens", "cost_usd"):
        item = value.get(key)
        if item is not None and (not isinstance(item, (int, float)) or item < 0):
            raise TraceError(f"agent.model.{key} must be a non-negative number or null")
        result[key] = item
    return result


def _transport(value: Any) -> dict[str, int | float | None]:
    keys = ("requests", "upload_bytes", "download_bytes", "request_seconds")
    if value is None:
        return {key: None for key in keys}
    if not isinstance(value, dict) or set(value) != set(keys):
        raise TraceError("agent.transport must contain request count, bytes, and request seconds")
    result: dict[str, int | float | None] = {}
    for key in keys:
        item = value[key]
        if item is not None and (not isinstance(item, (int, float)) or item < 0):
            raise TraceError(f"agent.transport.{key} must be a non-negative number or null")
        result[key] = item
    return result


def parse_trace(raw: Any, *, source: str = "<trace>", allow_fixture: bool = False) -> list[Workload]:
    if not isinstance(raw, dict) or raw.get("format") != TRACE_FORMAT:
        raise TraceError(f"{source}: unsupported trace format")
    if set(raw) != {"format", "trace_id", "recording", "workloads"}:
        raise TraceError(f"{source}: trace has unsupported fields")
    trace_id = raw["trace_id"]
    recording = raw["recording"]
    if not isinstance(trace_id, str) or not trace_id:
        raise TraceError(f"{source}: trace_id is required")
    if not isinstance(recording, dict) or set(recording) - {"kind", "label", "recorded_at"} or recording.get("kind") not in {"real-agent-cli", "real-agent-client", "fixture"}:
        raise TraceError(f"{source}: recording.kind must be real-agent-cli, real-agent-client, or fixture")
    origin = recording["kind"]
    if origin == "fixture" and not allow_fixture:
        raise TraceError(f"{source}: fixture traces require --allow-fixture and are not live-agent evidence")
    workloads = raw["workloads"]
    if not isinstance(workloads, list) or len(workloads) != len(GROUPS):
        raise TraceError(f"{source}: every trace must contain exactly the three pilot workload groups")
    parsed: list[Workload] = []
    seen_groups: set[str] = set()
    seen_ids: set[str] = set()
    for index, item in enumerate(workloads):
        expected = {"id", "group", "initial", "background", "agent", "oracle"}
        if not isinstance(item, dict) or set(item) != expected:
            raise TraceError(f"{source}: workloads[{index}] has unsupported fields")
        workload_id, group = item["id"], item["group"]
        if not isinstance(workload_id, str) or not workload_id or workload_id in seen_ids:
            raise TraceError(f"{source}: workload ids must be unique non-empty strings")
        if group not in GROUPS or group in seen_groups:
            raise TraceError(f"{source}: workload groups must be the three stable pilot groups")
        seen_ids.add(workload_id)
        seen_groups.add(group)
        initial = _paths(item["initial"], f"workloads[{index}].initial", allow_delete=False)
        background = _paths(item["background"], f"workloads[{index}].background", allow_delete=True)
        agent = item["agent"]
        if not isinstance(agent, dict) or set(agent) != {"actions", "proposal", "rework", "model", "transport"}:
            raise TraceError(f"{source}: workloads[{index}].agent has unsupported fields")
        actions = agent["actions"]
        if not isinstance(actions, list) or len(actions) > 10_000 or not all(isinstance(action, dict) for action in actions):
            raise TraceError(f"{source}: agent.actions must be a bounded list of objects")
        proposal = agent["proposal"]
        if not isinstance(proposal, dict) or set(proposal) != {"id", "edits", "observed"}:
            raise TraceError(f"{source}: agent.proposal requires id, edits, observed")
        proposal_id = proposal["id"]
        if proposal_id is not None and (not isinstance(proposal_id, str) or not proposal_id):
            raise TraceError(f"{source}: proposal.id must be a string or null")
        edits = _paths(proposal["edits"], f"workloads[{index}].agent.proposal.edits", allow_delete=True)
        observed = proposal["observed"]
        if not isinstance(observed, dict) or set(observed) != {"reads", "ranges"}:
            raise TraceError(f"{source}: proposal.observed requires reads and ranges")
        reads = observed["reads"]
        if not isinstance(reads, list) or len(reads) > 1_000 or not all(isinstance(path, str) for path in reads):
            raise TraceError(f"{source}: observed.reads must be a bounded string list")
        normalized_reads = tuple(sorted({_portable_path(path) for path in reads}))
        rework = agent["rework"]
        if not isinstance(rework, list) or len(rework) > 100 or not all(isinstance(event, dict) for event in rework):
            raise TraceError(f"{source}: agent.rework must be a bounded list of objects")
        oracle = item["oracle"]
        if not isinstance(oracle, dict) or set(oracle) != {"expected_safe", "reason"} or not isinstance(oracle["expected_safe"], bool) or not isinstance(oracle["reason"], str) or not oracle["reason"]:
            raise TraceError(f"{source}: oracle requires expected_safe and reason")
        parsed.append(Workload(
            trace_id, workload_id, group, dict(initial), background, edits,
            normalized_reads, _ranges(observed["ranges"]), tuple(actions), proposal_id,
            tuple(rework), oracle["expected_safe"], oracle["reason"], _model(agent["model"]),
            _transport(agent["transport"]), origin,
        ))
    if seen_groups != set(GROUPS):
        raise TraceError(f"{source}: missing a required pilot workload group")
    return parsed


def load_traces(paths: Iterable[Path], *, allow_fixture: bool = False, max_trace_bytes: int = MAX_TRACE_BYTES) -> list[Workload]:
    if not 1 <= max_trace_bytes <= MAX_TRACE_BYTES:
        raise TraceError(f"max trace bytes must be between 1 and {MAX_TRACE_BYTES}")
    workloads: list[Workload] = []
    seen_trace_ids: set[str] = set()
    for path in paths:
        try:
            size = path.stat().st_size
            if size > max_trace_bytes:
                raise TraceError(f"{path}: trace exceeds configured byte limit")
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise TraceError(f"Cannot read trace {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise TraceError(f"{path}: invalid JSON") from exc
        current = parse_trace(raw, source=str(path), allow_fixture=allow_fixture)
        if current[0].trace_id in seen_trace_ids:
            raise TraceError(f"Duplicate trace_id: {current[0].trace_id}")
        seen_trace_ids.add(current[0].trace_id)
        workloads.extend(current)
    return workloads


def _write(root: Path, changes: dict[str, bytes | None]) -> None:
    """Apply already-validated trace data without escaping ``root``.

    Revalidate here because this helper is also the last boundary before a
    filesystem mutation.  All targets are resolved first, so one malicious
    path cannot leave an earlier change partially materialized.
    """
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise TraceError(f"Cannot resolve materialization root {root}: {exc}") from exc
    if not resolved_root.is_dir():
        raise TraceError(f"Materialization root is not a directory: {root}")

    targets: list[tuple[Path, bytes | None]] = []
    for path, payload in changes.items():
        if payload is not None and not isinstance(payload, bytes):
            raise TraceError(f"Materialized payload for {path!r} must be bytes or null")
        portable_path = _portable_path(path)
        try:
            target = resolved_root.joinpath(*portable_path.split("/")).resolve(strict=False)
        except OSError as exc:
            raise TraceError(f"Cannot resolve materialized path {path!r}: {exc}") from exc
        if not target.is_relative_to(resolved_root):
            raise TraceError(f"Materialized path escapes its root: {path!r}")
        targets.append((target, payload))

    for target, payload in targets:
        if payload is None:
            if target.exists():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def _range_fingerprint(files: dict[str, bytes], kind: str, prefix: str) -> Any:
    # Keep conditional-Git's range semantics aligned with the existing
    # deterministic workflow replay instead of maintaining a second variant.
    return _workflow_fingerprint(files, kind, prefix)


def observations_match(workload: Workload) -> bool:
    current = {**workload.initial}
    for path, payload in workload.background.items():
        if payload is None:
            current.pop(path, None)
        else:
            current[path] = payload
    # Store._freeze captures expected objects for every effective write as well
    # as explicit reads.  Conditional Git must therefore reject a changed
    # output path even when a recorder did not list it among its reads.
    observed_paths = set(workload.edits) | set(workload.reads)
    if any(workload.initial.get(path) != current.get(path) for path in observed_paths):
        return False
    return all(
        _range_fingerprint(workload.initial, kind, prefix) == _range_fingerprint(current, kind, prefix)
        for kind, prefix in workload.ranges
    )


def _seed_replay(root: Path, workload: Workload) -> tuple[bool, dict[str, float], dict[str, int | float | None]]:
    phases: dict[str, float] = {}
    began = time.perf_counter()
    store = Store.create(root / "repo.db")
    store.land(store.propose(store.head(), workload.initial))
    base = store.head()
    phases["setup_seconds"] = time.perf_counter() - began
    began = time.perf_counter()
    proposal = store.propose(
        base, workload.edits, reads=workload.reads,
        ranges=[{"kind": kind, "prefix": prefix} for kind, prefix in workload.ranges],
    )
    phases["proposal_seconds"] = time.perf_counter() - began
    began = time.perf_counter()
    store.land(store.propose(base, workload.background))
    phases["background_seconds"] = time.perf_counter() - began
    began = time.perf_counter()
    try:
        store.land(proposal)
        accepted = True
    except Conflict:
        accepted = False
    phases["integration_seconds"] = time.perf_counter() - began
    return accepted, phases, {"requests": None, "upload_bytes": None, "download_bytes": None, "request_seconds": None}


def _git_call(root: Path, timeout: float, *arguments: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    # Git's environment variables can redirect even an apparently relative
    # command to another repository or index.  A comparison must never inherit
    # that caller state.  Disable hooks for every git invocation, including
    # init and maintenance commands, rather than only before commits.
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    result = subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "commit.gpgsign=false", *arguments],
        cwd=cwd or root, capture_output=True, text=True, encoding="utf-8",
        timeout=timeout, check=False, env=environment,
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git command failed")
    return result


def _git_replay(root: Path, workload: Workload, *, conditional: bool, command_timeout: float) -> tuple[bool, dict[str, float], dict[str, int | float | None]]:
    repo, task = root / "repo", root / "task"
    repo.mkdir()
    phases: dict[str, float] = {}
    began = time.perf_counter()
    _git_call(repo, command_timeout, "init", "--quiet", "--initial-branch=main")
    _git_call(repo, command_timeout, "config", "user.name", "Seed comparison")
    _git_call(repo, command_timeout, "config", "user.email", "comparison@example.invalid")
    _git_call(repo, command_timeout, "config", "core.autocrlf", "false")
    _git_call(repo, command_timeout, "config", "commit.gpgsign", "false")
    _git_call(repo, command_timeout, "config", "core.hooksPath", str(root / "no-hooks"))
    _write(repo, workload.initial)
    _git_call(repo, command_timeout, "add", "--all")
    _git_call(repo, command_timeout, "commit", "--quiet", "-m", "initial")
    _git_call(repo, command_timeout, "worktree", "add", "--quiet", "-b", "task", str(task), "HEAD")
    phases["setup_seconds"] = time.perf_counter() - began
    began = time.perf_counter()
    _write(task, workload.edits)
    _git_call(repo, command_timeout, "add", "--all", cwd=task)
    _git_call(repo, command_timeout, "commit", "--quiet", "-m", "recorded-agent-candidate", cwd=task)
    phases["proposal_seconds"] = time.perf_counter() - began
    began = time.perf_counter()
    _write(repo, workload.background)
    _git_call(repo, command_timeout, "add", "--all")
    _git_call(repo, command_timeout, "commit", "--quiet", "-m", "background")
    phases["background_seconds"] = time.perf_counter() - began
    began = time.perf_counter()
    if conditional and not observations_match(workload):
        accepted = False
    else:
        merged = _git_call(repo, command_timeout, "merge", "--no-edit", "--no-gpg-sign", "task", check=False)
        accepted = merged.returncode == 0
        if not accepted:
            _git_call(repo, command_timeout, "merge", "--abort")
    phases["integration_seconds"] = time.perf_counter() - began
    return accepted, phases, {"requests": 0, "upload_bytes": 0, "download_bytes": 0, "request_seconds": 0.0}


def _row(workload: Workload, backend: str, iteration: int, accepted: bool, phases: dict[str, float], transport: dict[str, int | float | None]) -> dict[str, Any]:
    return {
        "trace_id": workload.trace_id,
        "workload_id": workload.workload_id,
        "group": workload.group,
        "iteration": iteration,
        "backend": backend,
        "accepted": accepted,
        "expected_safe": workload.expected_safe,
        "oracle_reason": workload.oracle_reason,
        "unsafe_accept": accepted and not workload.expected_safe,
        "conservative_rejection": not accepted and workload.expected_safe,
        "observed": {"read_paths": len(workload.reads), "range_conditions": len(workload.ranges), "actions": len(workload.actions)},
        "recorded_agent": {
            "origin": workload.origin,
            "proposal_id": workload.proposal_id,
            "recorded_rework_events": len(workload.rework_events),
            "rework_required_by_replay": int(not accepted),
            "model": workload.model,
            "transport": workload.recorded_transport,
        },
        "phases": phases,
        "elapsed_seconds": sum(phases.values()),
        "transport": transport,
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for backend in BACKENDS:
        entries = [row for row in rows if row["backend"] == backend]
        elapsed = sorted(row["elapsed_seconds"] for row in entries)
        result[backend] = {
            "runs": len(entries),
            "accepted": sum(row["accepted"] for row in entries),
            "unsafe_accepts": sum(row["unsafe_accept"] for row in entries),
            "conservative_rejections": sum(row["conservative_rejection"] for row in entries),
            "reintegration_required": sum(row["recorded_agent"]["rework_required_by_replay"] for row in entries),
            "elapsed_seconds": {"p50": _percentile(elapsed, 0.5), "p95": _percentile(elapsed, 0.95)},
        }
    return result


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    return values[min(len(values) - 1, int((len(values) - 1) * fraction))]


def run(workloads: list[Workload], *, repeats: int = 5, command_timeout: float = 30.0, temp_root: Path | None = None) -> dict[str, Any]:
    if not workloads:
        raise TraceError("At least one trace is required")
    if not 1 <= repeats <= 20:
        raise TraceError("repeats must be between 1 and 20")
    if not 1 <= command_timeout <= 120:
        raise TraceError("command timeout must be between 1 and 120 seconds")
    temporary_parent = (temp_root or Path(__file__).resolve().parent / "artifacts" / "tmp").resolve()
    temporary_parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="agent-comparison-", dir=temporary_parent) as directory:
        root = Path(directory).resolve()
        if not root.is_relative_to(temporary_parent):
            raise RuntimeError("Comparison temporary workspace escaped its intended directory")
        for iteration in range(repeats):
            for workload in workloads:
                for backend in BACKENDS:
                    target = root / f"{iteration}-{workload.trace_id[:12]}-{workload.group}-{backend}"
                    target.mkdir()
                    if backend == "seed":
                        accepted, phases, transport = _seed_replay(target, workload)
                    else:
                        accepted, phases, transport = _git_replay(
                            target, workload, conditional=backend == "git-conditional-worktree", command_timeout=command_timeout,
                        )
                    rows.append(_row(workload, backend, iteration, accepted, phases, transport))
    origins = sorted({workload.origin for workload in workloads})
    return {
        "kind": "recorded-agent-trace-paired-replay",
        "repeats": repeats,
        "workload_groups": list(GROUPS),
        "backends": list(BACKENDS),
        "trace_origins": origins,
        "live_agent_productivity_measured": False,
        "limitations": [
            "Candidate edits and observed dependencies are replayed; models are not invoked.",
            "The safety oracle is hand-authored trace input and separate from observed reads.",
            "Seed uses an in-process Store in this first harness; request-byte metrics are null, not HTTP measurements.",
            "A rejected fixed replay reports rework required but does not fabricate a new model response.",
        ],
        "results": rows,
        "summary": _summary(rows),
    }


def _capture(spec_path: Path, output: Path, command: list[str], timeout: float) -> dict[str, Any]:
    """Run a user-selected recorder and validate its generic adapter result.

    This never selects a model or supplies solved edits.  The adapter receives
    the spec path in ``SEED_COMPARISON_SPEC`` (and ``{spec}`` placeholders are
    replaced in argv), writes JSON to stdout, and can report its own token/cost
    fields.  The supplied spec owns initial/background/oracle facts.
    """
    if not command:
        raise TraceError("capture requires an adapter command after --")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict) or set(spec) != {"format", "trace_id", "recording", "workloads"} or spec.get("format") != TRACE_FORMAT:
        raise TraceError("capture spec must use the comparison trace envelope")
    env = os.environ.copy()
    env.pop("SEED_TOKEN", None)
    env["SEED_COMPARISON_SPEC"] = str(spec_path.resolve())
    argv = [part.replace("{spec}", str(spec_path.resolve())) for part in command]
    completed = subprocess.run(argv, shell=False, env=env, capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=False)
    if completed.returncode:
        raise TraceError(f"adapter exited {completed.returncode}: {(completed.stderr or completed.stdout)[:500]}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise TraceError("adapter stdout must be one JSON object") from exc
    if not isinstance(result, dict) or set(result) != {"format", "workloads"} or result.get("format") != ADAPTER_FORMAT or not isinstance(result["workloads"], list):
        raise TraceError("adapter output has unsupported format")
    by_id = {entry.get("id"): entry for entry in result["workloads"] if isinstance(entry, dict)}
    if len(by_id) != len(spec["workloads"]):
        raise TraceError("adapter must return exactly one result for each spec workload")
    for workload in spec["workloads"]:
        adapted = by_id.get(workload["id"])
        if not isinstance(adapted, dict) or set(adapted) != {"id", "actions", "proposal", "rework", "model", "transport"}:
            raise TraceError("adapter workload result has unsupported fields")
        workload["agent"] = {key: adapted[key] for key in ("actions", "proposal", "rework", "model", "transport")}
    # Validation proves the adapter did not alter the independent oracle/base.
    parse_trace(spec, source=str(spec_path), allow_fixture=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"trace": str(output), "workloads": len(spec["workloads"]), "adapter_seconds": None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    replay = commands.add_parser("replay", help="Replay recorded traces against paired backends")
    replay.add_argument("--trace", action="append", nargs="+", required=True, type=Path)
    replay.add_argument("--repeats", type=int, default=5)
    replay.add_argument("--output", required=True, type=Path)
    replay.add_argument("--command-timeout", type=float, default=30)
    replay.add_argument("--max-trace-bytes", type=int, default=MAX_TRACE_BYTES)
    replay.add_argument("--allow-fixture", action="store_true")
    capture = commands.add_parser("capture", help="Invoke an explicitly supplied agent recorder adapter")
    capture.add_argument("--spec", required=True, type=Path)
    capture.add_argument("--output", required=True, type=Path)
    capture.add_argument("--timeout", type=float, default=120)
    capture.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    try:
        if arguments.action == "replay":
            paths = [path for group in arguments.trace for path in group]
            workloads = load_traces(paths, allow_fixture=arguments.allow_fixture, max_trace_bytes=arguments.max_trace_bytes)
            result = run(workloads, repeats=arguments.repeats, command_timeout=arguments.command_timeout)
        else:
            command = list(arguments.command)
            if command[:1] == ["--"]:
                command = command[1:]
            result = _capture(arguments.spec, arguments.output, command, arguments.timeout)
        if arguments.action == "replay":
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (TraceError, OSError, subprocess.SubprocessError, SeedError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
