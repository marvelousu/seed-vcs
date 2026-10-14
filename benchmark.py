"""Measure real HTTP submissions against an existing Seed service."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import platform
import time

from seed_client import Client
from seed import object_id


def percentile(values, proportion):
    values = sorted(values)
    return (
        values[min(len(values) - 1, int((len(values) - 1) * proportion))]
        if values
        else None
    )


def run(url, repository, token, *, files=100000, proposals=1000, clients=64, batch=8):
    if files < 0 or proposals < 1 or clients < 1 or not 1 <= batch <= 256:
        raise ValueError("Invalid benchmark dimensions")
    client = Client(url, repository, token)
    client.create()
    # Use a new empty repository for each run; no user files are overwritten.
    if client.files(limit=1)["files"]:
        raise ValueError("Benchmark requires an empty repository")
    seed_start = time.perf_counter()
    base = client.head()
    for start in range(0, files, 1000):
        edits = {
            f"source/{i:09}.txt": b"initial\n"
            for i in range(start, min(files, start + 1000))
        }
        proposal = client.propose(base, edits, message="benchmark initial files")
        base = client.land(proposal)["revision"]
    seeded_seconds = time.perf_counter() - seed_start
    frozen_base = base

    def submit(start):
        worker = Client(url, repository, token)
        timing = time.perf_counter()
        count = min(batch, proposals - start)
        identifiers = [
            worker.propose(
                frozen_base,
                {f"agents/{i:09}.txt": str(i).encode()},
                message="benchmark independent change",
            )
            for i in range(start, start + count)
        ]
        result = worker.land_many(identifiers)
        elapsed = time.perf_counter() - timing
        if any(r["status"] != "landed" for r in result):
            raise RuntimeError("Unexpected landing result: " + json.dumps(result))
        return count, elapsed

    wall_start = time.perf_counter()
    timings, successful, errors = [], 0, []
    with ThreadPoolExecutor(max_workers=clients) as pool:
        futures = [pool.submit(submit, i) for i in range(0, proposals, batch)]
        for future in as_completed(futures):
            try:
                count, elapsed = future.result()
                successful += count
                timings.append(elapsed)
            except Exception as exc:
                errors.append(str(exc))
    elapsed = time.perf_counter() - wall_start
    # Verify every landed path and all revision sequence numbers, page by page.
    paths, cursor, revision = 0, "", client.head()
    seen_paths = set()
    valid_metadata = True
    while True:
        page = client.files(revision=revision, after=cursor, limit=1000)
        paths += len(page["files"])
        for path, blob in page["files"].items():
            if path in seen_paths:
                valid_metadata = False
            seen_paths.add(path)
            kind, separator, number = path.partition("/")
            try:
                index = int(number.removesuffix(".txt"))
            except ValueError:
                valid_metadata = False
                continue
            if kind == "source" and 0 <= index < files:
                expected = object_id("blob", b"initial\n")
            elif kind == "agents" and 0 <= index < proposals:
                expected = object_id("blob", str(index).encode())
            else:
                valid_metadata = False
                continue
            if path != f"{kind}/{index:09}.txt" or blob != expected:
                valid_metadata = False
        if page["next"] is None:
            break
        cursor = page["next"]
    sequences, cursor = [], -1
    while True:
        page = client.history(after=cursor, limit=1000)
        sequences.extend(r["seq"] for r in page["revisions"])
        if page["next"] is None:
            break
        cursor = page["next"]
    expected_revisions = 1 + (files + 999) // 1000 + successful
    verified = (
        valid_metadata
        and paths == files + successful
        and sequences == list(range(expected_revisions))
    )
    for i in range(0, proposals, max(1, proposals // 20)):
        if (
            successful == proposals
            and client.read(f"agents/{i:09}.txt") != str(i).encode()
        ):
            verified = False
    return {
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "logical_cpus": os.cpu_count(),
        },
        "files_before": files,
        "proposals": proposals,
        "concurrent_clients": clients,
        "batch_size": batch,
        "seed_seconds": seeded_seconds,
        "elapsed_seconds": elapsed,
        "completed": successful,
        "proposals_per_second": successful / elapsed,
        "batch_latency_seconds": {
            "p50": percentile(timings, 0.5),
            "p95": percentile(timings, 0.95),
            "p99": percentile(timings, 0.99),
        },
        "errors": errors,
        "final_file_count": paths,
        "final_revision_count": len(sequences),
        "verified": verified,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7331")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--files", type=int, default=100000)
    parser.add_argument("--proposals", type=int, default=1000)
    parser.add_argument("--clients", type=int, default=64)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = run(
        args.url,
        args.repo,
        os.environ["SEED_TOKEN"],
        files=args.files,
        proposals=args.proposals,
        clients=args.clients,
        batch=args.batch,
    )
    payload = json.dumps(result, indent=2)
    print(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    return 0 if result["verified"] and not result["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
