"""Deterministic integration replay: native Seed, automated Git, conditional Git.

This compares acceptance decisions and local end-to-end integration costs, not
real-agent productivity or equivalent server throughput. Captured inputs are
explicit fixture observations; expected safety is a separate fixture oracle.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time

from seed import Conflict, Store


@dataclass
class Case:
    name: str
    initial: dict[str, bytes]
    writes: dict[str, bytes]
    background: dict[str, bytes]
    reads: tuple[str, ...] = ()
    ranges: tuple[tuple[str, str], ...] = ()
    safe: bool = True


def cases():
    lines = [f"line {i}\n" for i in range(60)]
    left, right = lines.copy(), lines.copy()
    left[3], right[53] = "edited by task\n", "edited in background\n"
    return [
        Case("independent", {"src/a": b"0", "docs/b": b"0"},
             {"src/a": b"1"}, {"docs/b": b"1"}, ("src/a",)),
        Case("same-file-different-lines", {"src/shared": "".join(lines).encode()},
             {"src/shared": "".join(left).encode()},
             {"src/shared": "".join(right).encode()}, ("src/shared",)),
        Case("changed-read-input", {"price": b"700"}, {"total": b"770"},
             {"price": b"800"}, ("price",), safe=False),
        Case("new-directory-member", {"items/a": b"10"}, {"total": b"10"},
             {"items/b": b"20"}, ("items/a",), (("membership", "items/"),), False),
        Case("new-search-match", {"src/a": b"nothing relevant"},
             {"report": b"no TODO found"}, {"src/a": b"TODO fix"},
             ranges=(("subtree", "src/"),), safe=False),
        Case("incidental-read", {"source": b"data", "notes": b"old comment"},
             {"output": b"DATA"}, {"notes": b"new comment"}, ("source", "notes")),
    ]


def fingerprint(files, kind, prefix):
    entries = sorted((path, hashlib.sha256(data).hexdigest())
                     for path, data in files.items() if path.startswith(prefix))
    return [path for path, _ in entries] if kind == "membership" else entries


def conditions_match(case):
    current = {**case.initial, **case.background}
    paths = set(case.writes) | set(case.reads)
    return (all(case.initial.get(path) == current.get(path) for path in paths)
            and all(fingerprint(case.initial, kind, prefix) ==
                    fingerprint(current, kind, prefix) for kind, prefix in case.ranges))


def native(root, case):
    store = Store.create(root / "repo.db")
    store.land(store.propose(store.head(), case.initial))
    base = store.head()
    proposal = store.propose(base, case.writes, reads=case.reads,
                             ranges=[{"kind": kind, "prefix": prefix}
                                     for kind, prefix in case.ranges])
    store.land(store.propose(base, case.background))
    try:
        store.land(proposal)
        accepted = True
    except Conflict:
        accepted = False
    return accepted


def git(root, case, conditional=False):
    repo, work = root / "repo", root / "task"
    repo.mkdir()

    def call(*args, cwd=repo, check=True):
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                                text=True, encoding="utf-8", check=False,
                                timeout=30)
        if check and result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result

    def write(directory, edits):
        for path, payload in edits.items():
            destination = directory / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)

    call("init", "--quiet", "--initial-branch=main")
    call("config", "user.name", "Seed replay")
    call("config", "user.email", "replay@example.invalid")
    call("config", "core.autocrlf", "false")
    call("config", "commit.gpgsign", "false")
    call("config", "core.hooksPath", str(root / "no-hooks"))
    write(repo, case.initial)
    call("add", "--all")
    call("commit", "--quiet", "-m", "initial")
    call("worktree", "add", "--quiet", "-b", "task", str(work), "HEAD")
    write(work, case.writes)
    call("add", "--all", cwd=work)
    call("commit", "--quiet", "-m", "task", cwd=work)
    write(repo, case.background)
    call("add", "--all")
    call("commit", "--quiet", "-m", "background")
    if conditional and not conditions_match(case):
        return False
    result = call("merge", "--no-edit", "--no-gpg-sign", "task", check=False)
    if result.returncode:
        unresolved = call("diff", "--name-only", "--diff-filter=U").stdout.strip()
        if not unresolved:
            raise RuntimeError("Git merge failed without a content conflict: " + result.stderr)
        call("merge", "--abort")
        return False
    return True


def run(repeats=1):
    if not 1 <= repeats <= 20:
        raise ValueError("repeats must be between 1 and 20")
    temporary_root = Path(__file__).resolve().parent / "artifacts" / "tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix="workflow-", dir=temporary_root) as directory:
        root = Path(directory).resolve()
        if not root.is_relative_to(temporary_root.resolve()):
            raise RuntimeError("Temporary workspace escaped its intended directory")
        for iteration in range(repeats):
            for case in cases():
                for engine in ("seed", "git", "conditional-git"):
                    target = root / f"{iteration}-{case.name}-{engine}"
                    target.mkdir()
                    start = time.perf_counter()
                    accepted = (native(target, case) if engine == "seed" else
                                git(target, case, conditional=engine == "conditional-git"))
                    rows.append({"case": case.name, "engine": engine,
                                 "iteration": iteration, "accepted": accepted,
                                 "expected_safe": case.safe,
                                 "unsafe_accept": accepted and not case.safe,
                                 "conservative_rejection": not accepted and case.safe,
                                 "observed_read_paths": len(set(case.reads)),
                                 "range_conditions": len(case.ranges),
                                 "elapsed_seconds": time.perf_counter() - start})
    return {"kind": "deterministic-local-replay", "repeats": repeats,
            "limitations": ["Fixture read sets, not captured live-agent traces",
                            "No model/token cost or server throughput measurement",
                            "Local implementation/setup costs differ between engines"],
            "results": rows,
            "summary": {engine: {
                "accepted": sum(r["accepted"] for r in rows if r["engine"] == engine),
                "unsafe_accepts": sum(r["unsafe_accept"] for r in rows if r["engine"] == engine),
                "conservative_rejections": sum(r["conservative_rejection"] for r in rows if r["engine"] == engine)
            } for engine in ("seed", "git", "conditional-git")}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = run(args.repeats)
    rendered = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
