"""Publication-cost comparison for Seed and a bare conditional-Git publisher.

This is deliberately a local, deterministic experiment.  It measures neither
model quality nor network transfer.  Git workers are represented by immutable
prepared blobs and predicates; publication uses a single ``fast-import`` process per batch,
with an in-memory predicate cache under a documented sole-writer assumption.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any

from seed import Store, canonical, object_id


ROOT = Path(__file__).resolve().parent
TMP = ROOT / "artifacts" / "tmp"
FORMAT = "seed-wide-tree-publication/1"
MAX_WIDTH = 32_768
MAX_PROPOSALS = 32


@dataclass(frozen=True)
class Proposal:
    number: int
    path: str
    payload: bytes
    seed_id: str
    seed_expected: dict[str, str | None]
    git_blob: str
    git_expected: dict[str, str]
    audit: bytes
    git_audit_blob: str


@dataclass
class Prepared:
    layout: str
    width: int
    seed_template: Path
    git_template: Path
    seed_background: str
    seed_base_history: int
    git_base: str
    git_background: str
    initial: dict[str, bytes]
    git_initial: dict[str, str]
    proposals: list[Proposal]
    control_path: str
    control_after: bytes
    control_after_blob: str


def _environment() -> dict[str, str]:
    result = {key: value for key, value in os.environ.items()
              if not key.upper().startswith("GIT_")}
    result.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_ATTR_NOSYSTEM": "1"})
    return result


def _git_version() -> tuple[str, tuple[int, int, int]]:
    """Return and enforce the Git version required by the fast-import protocol."""
    raw = _git(ROOT, "--version").stdout.decode("utf-8", "replace").strip()
    matched = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", raw)
    if matched is None:
        raise RuntimeError("could not determine Git version: " + raw)
    version = tuple(int(part or 0) for part in matched.groups())
    if version < (2, 43, 0):
        raise RuntimeError("wide-tree benchmark requires Git 2.43 or newer, got " + raw)
    return raw, version


def _source_metadata() -> dict[str, Any]:
    """Identify this module whether it runs from a checkout or an installed wheel."""
    commit_result = _git(ROOT, "rev-parse", "HEAD", check=False)
    status_result = _git(ROOT, "status", "--porcelain", check=False)
    in_checkout = commit_result.returncode == 0 and status_result.returncode == 0
    return {"commit": commit_result.stdout.decode().strip() if in_checkout else None,
            "dirty": bool(status_result.stdout) if in_checkout else None,
            "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def _git_blob_id(payload: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()


def _content_digest(files: dict[str, bytes]) -> str:
    body = [[path, hashlib.sha256(files[path]).hexdigest()] for path in sorted(files)]
    return hashlib.sha256(canonical(body)).hexdigest()


def _git(root: Path, *arguments: str, data: bytes | None = None,
         check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}",
        "-c", "commit.gpgsign=false", "-c", "core.fsync=all",
         "-c", "core.fsyncMethod=fsync", "-c", "fastimport.unpackLimit=1",
         "-c", "gc.auto=0", *arguments],
        cwd=root, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=_environment(), check=False, timeout=120,
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    return result


def _cpu() -> tuple[float | None, float | None]:
    if os.name == "nt":
        return None, None
    import resource
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime, children.ru_utime + children.ru_stime


def _file_path(index: int, layout: str) -> str:
    name = f"item-{index:05d}.json"
    if layout == "flat":
        return "generated/ui/" + name
    return f"generated/ui/{index // 128:03d}/" + name


def _initial(width: int, layout: str) -> dict[str, bytes]:
    files = {_file_path(index, layout): f'{{"base":{index}}}\n'.encode()
             for index in range(width)}
    files["controls/input.json"] = b'{"revision":"base"}\n'
    return files


def _audit(number: int, base: str, expected: dict[str, str | None], writes: dict[str, str]) -> bytes:
    return ("seed-wide-tree-audit/1\nproposal=" + str(number) + "\nbase=" + base
            + "\nexpected=" + json.dumps(expected, sort_keys=True, separators=(",", ":"))
            + "\nwrites=" + json.dumps(writes, sort_keys=True, separators=(",", ":"))
            + "\n").encode()


def _fast_import(root: Path, stream: bytes, *, marks: Path | None = None) -> dict[str, str]:
    command = ["git", "-c", f"core.hooksPath={os.devnull}",
               "-c", "commit.gpgsign=false", "-c", "core.fsync=all",
               "-c", "core.fsyncMethod=fsync", "-c", "fastimport.unpackLimit=1",
               "-c", "gc.auto=0", "fast-import", "--quiet"]
    if marks is not None:
        command.append("--export-marks=" + str(marks))
    result = subprocess.run(command, cwd=root, input=b"feature done\n" + stream + b"done\n", stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=_environment(), check=False, timeout=120)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    if marks is None or not marks.exists():
        return {}
    result: dict[str, str] = {}
    for line in marks.read_text(encoding="ascii").splitlines():
        mark, identifier = line.split(" ", 1)
        result[mark] = identifier
    return result


def _commit(ref: str, parent: str | None, message: bytes,
            changes: list[tuple[str, str]]) -> bytes:
    parts = [f"commit {ref}\n".encode(),
             b"committer Wide Tree Benchmark <benchmark@example.invalid> 0 +0000\n",
             f"data {len(message)}\n".encode(), message]
    if parent:
        parts.append(b"from " + parent.encode() + b"\n")
    for path, blob in changes:
        parts.append(b"M 100644 " + blob.encode() + b" " + path.encode() + b"\n")
    parts.append(b"\n")
    return b"".join(parts)


def _tree(root: Path, revision: str) -> dict[str, str]:
    raw = _git(root, "ls-tree", "-r", "-z", revision).stdout
    result: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        left, path = item.split(b"\t", 1)
        mode, kind, identifier = left.split(b" ")
        if mode != b"100644" or kind != b"blob":
            raise RuntimeError("benchmark accepts regular files only")
        result[path.decode()] = identifier.decode()
    return result


def _hash_blob(root: Path, payload: bytes) -> str:
    return _git(root, "hash-object", "-w", "--stdin", data=payload).stdout.strip().decode()


def _checkpoint(path: Path) -> None:
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        mode = db.execute("PRAGMA journal_mode").fetchone()[0].lower()
    finally:
        db.close()
    if mode != "wal":
        raise RuntimeError(f"Seed SQLite must be WAL, got {mode}")


def prepare(root: Path, width: int, layout: str, proposals: int) -> Prepared:
    root.mkdir(parents=True, exist_ok=True)
    initial = _initial(width, layout)
    seed_template = root / "seed-template.db"
    store = Store.create(seed_template)
    initial_id = store.propose(store.head(), initial, message="wide tree base")
    base = store.land(initial_id)["revision"]
    seed_base_history = len(store.log())
    control = "controls/input.json"
    prepared: list[Proposal] = []
    for number in range(proposals):
        path = _file_path(number * width // proposals, layout)
        payload = f'{{"generatedBy":{number}}}\n'.encode()
        reads = [control] if number == 0 else []
        proposal_id = store.propose(base, {path: payload}, reads=reads,
                                    author="benchmark", message=f"proposal {number}")
        body = store.describe(proposal_id)
        prepared.append(Proposal(number, path, payload, proposal_id,
                                 dict(body["expected"]), "", {}, b"", ""))
    background = store.propose(base, {control: b'{"revision":"changed"}\n'},
                               message="background control update")
    _checkpoint(seed_template)

    git_template = root / "git-template"
    _git(root, "init", "--bare", "--object-format=sha1", str(git_template))
    base_stream = _commit("refs/heads/main", None, b"wide tree base\n", [])
    # Add the initial files to the base commit.  Inline data makes setup independent
    # from worktree/index behavior and remains outside every timed window.
    base_stream = base_stream[:-1]
    for path, payload in initial.items():
        base_stream += f"M 100644 inline {path}\n".encode()
        base_stream += f"data {len(payload)}\n".encode() + payload + b"\n"
    base_stream += b"\n"
    _fast_import(git_template, base_stream)
    git_base = _git(git_template, "rev-parse", "refs/heads/main").stdout.strip().decode()
    git_initial = _tree(git_template, git_base)
    control_after = b'{"revision":"changed"}\n'
    control_after_blob = _hash_blob(git_template, control_after)
    git_background_stream = _commit("refs/bench/prepared/background", git_base,
                                    b"background control update\n",
                                    [(control, control_after_blob)])
    _fast_import(git_template, git_background_stream)
    git_background = _git(git_template, "rev-parse", "refs/bench/prepared/background").stdout.strip().decode()
    final: list[Proposal] = []
    for proposal in prepared:
        blob = _hash_blob(git_template, proposal.payload)
        expected = {proposal.path: git_initial[proposal.path]}
        if proposal.number == 0:
            expected[control] = git_initial[control]
        audit = _audit(proposal.number, git_base, expected, {proposal.path: blob})
        audit_blob = _hash_blob(git_template, audit)
        final.append(Proposal(proposal.number, proposal.path, proposal.payload,
                              proposal.seed_id, proposal.seed_expected, blob, expected, audit, audit_blob))
    pending_commands = ["start"]
    for proposal in final:
        pending_commands.extend([
            f"update refs/bench/pending/audit-{proposal.number:02d} {proposal.git_audit_blob}",
            f"update refs/bench/pending/payload-{proposal.number:02d} {proposal.git_blob}",
        ])
    pending_commands.extend(["prepare", "commit", ""])
    _git(git_template, "update-ref", "--stdin", data="\n".join(pending_commands).encode())
    return Prepared(layout, width, seed_template, git_template, background, seed_base_history, git_base,
                    git_background, initial, git_initial, final, control, control_after,
                    control_after_blob)


def _copy_seed(prepared: Prepared, root: Path) -> Store:
    target = root / "seed.db"
    shutil.copy2(prepared.seed_template, target)
    return Store(target)


def _copy_git(prepared: Prepared, root: Path) -> Path:
    target = root / "git"
    shutil.copytree(prepared.git_template, target)
    _git(target, "update-ref", "refs/heads/main", prepared.git_background, prepared.git_base)
    return target


def _chunks(values: list[Proposal], size: int) -> list[list[Proposal]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def _seed_durability(store: Store) -> dict[str, Any]:
    """Check the actual Store connection configuration before its timed calls."""
    with store._db(write=True) as db:
        result = {"journal_mode": db.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                  "synchronous": db.execute("PRAGMA synchronous").fetchone()[0]}
    if result != {"journal_mode": "wal", "synchronous": 2}:
        raise RuntimeError("Seed Store connection is not WAL/FULL: " + repr(result))
    return result


def _flush_tree(root: Path) -> float:
    """Persist setup files before opening the publication timing window."""
    began = time.perf_counter()
    # Windows does not expose a portable fsync operation for Git's read-only
    # object files.  The POSIX barrier is therefore reported as unavailable
    # there instead of silently switching to a system-wide cache flush.
    if os.name == "nt":
        return time.perf_counter() - began
    files: list[Path] = []
    directories: set[Path] = set()
    for directory, _, names in os.walk(root):
        current = Path(directory)
        directories.add(current)
        files.extend(current / name for name in names)
    for path in files:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    if os.name != "nt":
        for path in directories:
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    return time.perf_counter() - began


def _select_accepted(group: list[Proposal], current: dict[str, str]) -> tuple[list[Proposal], list[dict[str, Any]]]:
    """Apply conditional proposals in order, matching Seed ``land_many`` semantics."""
    accepted: list[Proposal] = []
    decisions: list[dict[str, Any]] = []
    for item in group:
        matches = all(current.get(path) == expected for path, expected in item.git_expected.items())
        decisions.append({"proposal": item.number, "accepted": matches,
                          "status": "landed" if matches else "conflict",
                          "git_audit_blob": item.git_audit_blob, "audit": item.audit.decode()})
        if matches:
            accepted.append(item)
            current[item.path] = item.git_blob
    return accepted, decisions


def _seed_publish(prepared: Prepared, target: Path, batch: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    setup_began = time.perf_counter()
    store = _copy_seed(prepared, target)
    store.land(prepared.seed_background)  # advancing head is deliberately outside timing
    durability = _seed_durability(store)
    flush_seconds = _flush_tree(target)
    setup_seconds = time.perf_counter() - setup_began
    baseline_storage = _sqlite_stats(store.database)
    parent_cpu, child_cpu = _cpu()
    began = time.perf_counter()
    decisions: list[dict[str, Any]] = []
    for group in _chunks(prepared.proposals, batch):
        for proposal, result in zip(group, store.land_many([item.seed_id for item in group]), strict=True):
            decisions.append({"proposal": proposal.number, "accepted": result["status"] == "landed",
                              "status": result["status"], "seed_proposal": proposal.seed_id,
                              "audit": proposal.audit.decode()})
    elapsed = time.perf_counter() - began
    parent_after, child_after = _cpu()
    report = _verify_seed(prepared, store, decisions)
    before = _sqlite_stats(store.database)
    maintenance_began = time.perf_counter()
    _vacuum(store.database)
    maintenance_seconds = time.perf_counter() - maintenance_began
    report["storage"] = {"before_publication": baseline_storage, "before_maintenance": before,
                         "after_maintenance": _sqlite_stats(store.database)}
    report["metrics"] = {"setup_seconds": setup_seconds, "flush_seconds": flush_seconds,
                         "elapsed_seconds": elapsed, "maintenance_seconds": maintenance_seconds,
                         "parent_cpu_seconds": _delta(parent_cpu, parent_after),
                         "child_cpu_seconds": _delta(child_cpu, child_after), "subprocesses": 0,
                         "cpu_accounting": "rusage" if os.name != "nt" else "unavailable",
                         "flush_barrier": "file-and-directory-fsync" if os.name != "nt" else "unavailable"}
    report["seed_durability"] = durability
    return decisions, report


def _git_publish(prepared: Prepared, target: Path, batch: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    setup_began = time.perf_counter()
    repo = _copy_git(prepared, target)
    current = dict(prepared.git_initial)
    current[prepared.control_path] = prepared.control_after_blob
    parent = prepared.git_background
    effective = {key: _git(repo, "config", "--get", key).stdout.decode().strip()
                 for key in ("core.fsync", "core.fsyncMethod", "fastimport.unpackLimit", "gc.auto")}
    if effective != {"core.fsync": "all", "core.fsyncMethod": "fsync",
                     "fastimport.unpackLimit": "1", "gc.auto": "0"}:
        raise RuntimeError("Git effective config differs from benchmark contract: " + repr(effective))
    if _git(repo, "rev-parse", "--show-object-format").stdout.strip() != b"sha1":
        raise RuntimeError("wide-tree benchmark requires SHA-1 Git objects")
    flush_seconds = _flush_tree(target)
    setup_seconds = time.perf_counter() - setup_began
    baseline_storage = _git_storage(repo)
    parent_cpu, child_cpu = _cpu()
    began = time.perf_counter()
    decisions: list[dict[str, Any]] = []
    processes = 0
    for batch_index, group in enumerate(_chunks(prepared.proposals, batch)):
        accepted, batch_decisions = _select_accepted(group, current)
        decisions.extend(batch_decisions)
        marks = target / f"marks-{batch_index}.txt"
        stream = b""
        previous = parent
        mark = 1
        for item in accepted:
            stream += b"commit refs/bench/staging\nmark :" + str(mark).encode() + b"\n"
            stream += _commit("", previous, item.audit, [(item.path, item.git_blob)])[len(b"commit \n"):]
            previous = ":" + str(mark)
            mark += 1
        if accepted:
            stream += b"reset refs/heads/main\nfrom " + previous.encode() + b"\n\n"
            stream += b"reset refs/bench/staging\n\n"
        if accepted:
            _fast_import(repo, stream, marks=marks)
            processes += 1
            parent = _fast_import_mark(marks, ":" + str(mark - 1))
    elapsed = time.perf_counter() - began
    parent_after, child_after = _cpu()
    report = _verify_git(prepared, repo, decisions, parent)
    before = _git_storage(repo)
    maintenance_began = time.perf_counter()
    _git(repo, "gc", "--prune=now")
    maintenance_seconds = time.perf_counter() - maintenance_began
    _verify_pending_git_objects(repo, prepared.proposals)
    report["storage"] = {"before_publication": baseline_storage, "before_maintenance": before,
                         "after_maintenance": _git_storage(repo),
                         "new_reachable_tree_bytes": _new_tree_bytes(repo, prepared.git_background, parent)}
    report["metrics"] = {"setup_seconds": setup_seconds, "flush_seconds": flush_seconds,
                         "elapsed_seconds": elapsed, "maintenance_seconds": maintenance_seconds,
                         "parent_cpu_seconds": _delta(parent_cpu, parent_after),
                         "child_cpu_seconds": _delta(child_cpu, child_after), "subprocesses": processes,
                         "cpu_accounting": "rusage" if os.name != "nt" else "unavailable",
                         "flush_barrier": "file-and-directory-fsync" if os.name != "nt" else "unavailable"}
    report["git_effective_config"] = effective
    return decisions, report


def _fast_import_mark(path: Path, mark: str) -> str:
    for line in path.read_text(encoding="ascii").splitlines():
        if line.startswith(mark + " "):
            return line.split(" ", 1)[1]
    raise RuntimeError("fast-import did not export final commit mark")


def _delta(before: float | None, after: float | None) -> float | None:
    return None if before is None or after is None else after - before


def _expected(prepared: Prepared, decisions: list[dict[str, Any]]) -> dict[str, bytes]:
    result = dict(prepared.initial)
    result[prepared.control_path] = prepared.control_after
    by_number = {item.number: item for item in prepared.proposals}
    for decision in decisions:
        if decision["accepted"]:
            item = by_number[decision["proposal"]]
            result[item.path] = item.payload
    return result


def _verify_decisions(decisions: list[dict[str, Any]], count: int) -> None:
    if len(decisions) != count or [item["proposal"] for item in decisions] != list(range(count)):
        raise AssertionError("publication decisions are not a complete ordered proposal set")
    if any(item["accepted"] != (item["proposal"] != 0) for item in decisions):
        raise AssertionError("oracle requires exactly one rejected stale-control proposal")
    if (decisions[0]["proposal"], decisions[0]["accepted"], decisions[0]["status"]) != (0, False, "conflict"):
        raise AssertionError("control-reading proposal was not rejected")


def _verify_seed(prepared: Prepared, store: Store, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    _verify_decisions(decisions, len(prepared.proposals))
    expected = _expected(prepared, decisions)
    manifest = store.manifest()
    expected_ids = {path: object_id("blob", data) for path, data in expected.items()}
    if manifest != expected_ids:
        raise AssertionError("Seed final manifest differs from oracle")
    history = store.log()
    if len(history) != prepared.seed_base_history + 1 + sum(item["accepted"] for item in decisions):
        raise AssertionError("Seed history lost or added a publication")
    for item in prepared.proposals:
        body = store.describe(item.seed_id)
        if body["expected"] != item.seed_expected:
            raise AssertionError("Seed proposal audit changed")
    previous = history[prepared.seed_base_history - 1]["id"]
    background = history[prepared.seed_base_history]
    if background["parent"] != previous or background["proposal"] != prepared.seed_background:
        raise AssertionError("Seed background did not advance the prepared base")
    previous = background["id"]
    accepted = [item for item in prepared.proposals if item.number != 0]
    for entry, proposal in zip(history[prepared.seed_base_history + 1:], accepted, strict=True):
        if entry["parent"] != previous or entry["proposal"] != proposal.seed_id:
            raise AssertionError("Seed publication history changed its proposal lineage")
        if store.read(proposal.path, entry["id"]) != proposal.payload:
            raise AssertionError("Seed accepted revision lacks its published content")
        previous = entry["id"]
    return {"final_digest": _content_digest(expected),
            "accepted": sum(item["accepted"] for item in decisions), "history": len(history)}


def _verify_git(prepared: Prepared, repo: Path, decisions: list[dict[str, Any]], head: str) -> dict[str, Any]:
    _verify_decisions(decisions, len(prepared.proposals))
    if _git(repo, "rev-parse", "refs/heads/main").stdout.strip().decode() != head:
        raise AssertionError("Git main differs from the fast-import final mark")
    expected_payload = _expected(prepared, decisions)
    expected = {path: _git_blob_id(payload) for path, payload in expected_payload.items()}
    by_number = {item.number: item for item in prepared.proposals}
    tree = _tree(repo, head)
    if tree != expected:
        raise AssertionError("Git final tree differs from oracle")
    rows = _git(repo, "rev-list", "--reverse", "--parents", f"{prepared.git_background}..{head}").stdout.splitlines()
    entries = [tuple(line.split(b" ", 1)) for line in rows]
    accepted = [by_number[item["proposal"]] for item in decisions if item["accepted"]]
    if len(entries) != len(accepted):
        raise AssertionError("Git publication history is incomplete or unaudited")
    previous = prepared.git_background.encode()
    for (identifier, parent), proposal in zip(entries, accepted, strict=True):
        message = _git(repo, "cat-file", "-p", identifier.decode()).stdout.partition(b"\n\n")[2]
        if parent != previous or message != proposal.audit:
            raise AssertionError("Git commit lineage or audit differs from prepared proposal")
        if _git(repo, "cat-file", "-p", f"{identifier.decode()}:{proposal.path}").stdout != proposal.payload:
            raise AssertionError("Git accepted commit lacks its published content")
        previous = identifier
    rejected = prepared.proposals[0]
    if _git(repo, "cat-file", "-p", rejected.git_audit_blob).stdout != rejected.audit:
        raise AssertionError("Git rejected proposal audit blob was not preserved")
    if _git(repo, "show-ref", "--verify", "--quiet", "refs/bench/staging", check=False).returncode == 0:
        raise AssertionError("fast-import staging ref remained visible")
    return {"final_digest": _content_digest(expected_payload),
            "accepted": sum(item["accepted"] for item in decisions), "history": len(entries)}


def _verify_pending_git_objects(repo: Path, proposals: list[Proposal]) -> None:
    for proposal in proposals:
        if (_git(repo, "cat-file", "-p", proposal.git_audit_blob).stdout != proposal.audit
                or _git(repo, "cat-file", "-p", proposal.git_blob).stdout != proposal.payload):
            raise AssertionError("Git pending proposal input was collected by maintenance")


def _sqlite_stats(path: Path) -> dict[str, int]:
    db = sqlite3.connect(path)
    try:
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        versions = db.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
        revisions = db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
    finally:
        db.close()
    return {"repository_physical_bytes": path.stat().st_size + sum(item.stat().st_size for item in path.parent.glob(path.name + "-*")),
            "database_bytes": pages * page_size, "versions": versions, "revisions": revisions}


def _vacuum(path: Path) -> None:
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("VACUUM")
    finally:
        db.close()


def _git_storage(repo: Path) -> dict[str, int]:
    return {"repository_physical_bytes": sum(item.stat().st_size for item in repo.rglob("*") if item.is_file())}


def _new_tree_bytes(repo: Path, base: str, head: str) -> int:
    objects = _git(repo, "rev-list", "--objects", "--no-object-names", f"{base}..{head}").stdout
    if not objects:
        return 0
    rows = _git(repo, "cat-file", "--batch-check=%(objecttype) %(objectsize)", data=objects).stdout.splitlines()
    return sum(int(size) for kind, size in (row.split(b" ", 1) for row in rows) if kind == b"tree")


def _quartiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {"median": statistics.median(ordered),
            "iqr": statistics.quantiles(ordered, n=4, method="inclusive")[2]
                   - statistics.quantiles(ordered, n=4, method="inclusive")[0] if len(ordered) > 1 else 0.0}


def run(*, widths: list[int], batches: list[int], repeats: int, proposals: int,
        temporary_parent: Path | None = None) -> dict[str, Any]:
    _validate(widths, batches, repeats, proposals)
    git_version, _ = _git_version()
    source = _source_metadata()
    parent = (temporary_parent or TMP).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    preparation: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="wide-tree-", dir=parent) as directory:
        root = Path(directory)
        layouts = [("flat", width) for width in widths] + [("sharded", max(widths))]
        for layout_index, (layout, width) in enumerate(layouts):
            print(f"[wide-tree] prepare layout={layout} width={width}", file=sys.stderr, flush=True)
            began = time.perf_counter()
            prepared = prepare(root / f"prepared-{layout}-{width}", width, layout, proposals)
            preparation.append({"layout": layout, "width": width,
                                "seconds": time.perf_counter() - began})
            for batch in batches:
                for repeat in range(repeats):
                    order = ("seed", "git") if (layout_index + repeat) % 2 == 0 else ("git", "seed")
                    for backend in order:
                        print(f"[wide-tree] layout={layout} width={width} batch={batch} "
                              f"repeat={repeat + 1}/{repeats} backend={backend}",
                              file=sys.stderr, flush=True)
                        target = root / f"{layout}-{width}-{batch}-{repeat}-{backend}"
                        target.mkdir()
                        decisions, details = (_seed_publish(prepared, target, batch)
                                              if backend == "seed" else _git_publish(prepared, target, batch))
                        rows.append({"layout": layout, "width": width, "batch": batch,
                                     "repeat": repeat, "backend": backend, "decisions": decisions,
                                     **details})
    summary: dict[str, Any] = {}
    for row in rows:
        key = f"{row['layout']}/{row['width']}/batch-{row['batch']}/{row['backend']}"
        summary.setdefault(key, []).append(row["metrics"]["elapsed_seconds"])
    return {"format": FORMAT, "environment": {"platform": platform.platform(),
            "python": sys.version, "sqlite": sqlite3.sqlite_version, "git": git_version,
            "source": source, "cpu_count": os.cpu_count(),
            "processor": platform.processor() or None,
            "temporary_filesystem": {"root": parent.anchor, "os_name": os.name},
            "git_durability": {"core.fsync": "all", "core.fsyncMethod": "fsync",
                               "hooks": "disabled", "signing": "disabled", "object_format": "sha1"}},
            "configuration": {"widths": widths, "batches": batches,
            "repeats": repeats, "proposals": proposals, "sole_writer": True,
            "timing_excludes": ["base/proposal preparation", "template copying", "background advance",
                                "Git gc", "SQLite vacuum", "verification"],
            "not_measured": ["network transfer", "model productivity", "multi-writer CAS contention"]},
            "preparation": preparation, "rows": rows,
            "summary": {key: _quartiles(value) for key, value in summary.items()}}


def _validate(widths: list[int], batches: list[int], repeats: int, proposals: int) -> None:
    if not widths or any(not 1 <= value <= MAX_WIDTH for value in widths) or len(set(widths)) != len(widths):
        raise ValueError(f"widths must be distinct integers from 1 to {MAX_WIDTH}")
    if (not batches or len(set(batches)) != len(batches)
            or any(not 1 <= value <= MAX_PROPOSALS for value in batches)):
        raise ValueError(f"batches must be distinct integers from 1 to {MAX_PROPOSALS}")
    if not 1 <= proposals <= MAX_PROPOSALS or min(widths) < proposals or max(batches) > proposals:
        raise ValueError(f"proposals must be 1..{MAX_PROPOSALS} and no wider than every layout")
    if not 1 <= repeats <= 21:
        raise ValueError("repeats must be 1..21")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--widths", type=int, nargs="+", default=[128, 4096, 32768])
    parser.add_argument("--batches", type=int, nargs="+", default=[1, 8, 32])
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--proposals", type=int, default=32)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run(widths=args.widths, batches=args.batches, repeats=args.repeats,
                     proposals=args.proposals)
    except (ValueError, RuntimeError, AssertionError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
