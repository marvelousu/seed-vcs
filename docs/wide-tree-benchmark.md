# Wide-directory publication experiment

This experiment tests a narrow possible advantage: publishing many independent
one-file changes into a directory with thousands of sibling files, while keeping
one independently auditable revision per accepted change. Generated assets, UI
definitions, and generated configuration are examples of this repository shape.

It compares the existing SQLite Seed store with a conditional Git adapter built
on a bare repository and `git fast-import`. The Git baseline has no clone,
checkout, staging index, or per-commit CLI startup. It keeps a warm in-memory
condition cache under a sole-writer assumption, checks the same read/write
conditions, and uses one importer process per batch. This deliberately gives Git
an efficient integration service rather than comparing Seed against repeated
interactive rebases.

## Predeclared hypothesis and controls

Seed indexes current paths and per-path history. Git stores directory entries in
tree objects referenced by each commit. The hypothesis is that repeatedly
representing a wide directory can add publication cost even when the changed
payload is small. Git's normal tree deltas and pack compression remain enabled;
raw tree bytes are not physical storage or network bytes.

The matrix uses flat directories of 128, 4,096, and 32,768 files plus a sharded
control with the same 32,768 files and at most 128 siblings per leaf. Each case
freezes 32 proposals at one base. One proposal reads an input that a background
revision then changes; that proposal must be rejected. The other 31 edit distinct
paths, spread across the directory or shards, and must be accepted. Both engines
must preserve all accepted intermediate revisions and their original conditions.
Batch sizes are 1, 8, and 32, with seven repeats and alternating engine order.
All results, including losses, belong in the report.

## Measurement boundary

Base creation, blob upload, immutable proposal preparation, repository copies,
verification, and maintenance are outside the publication window. Preparation
time is reported separately. The timed work includes predicate checks and
publication. Seed uses WAL with synchronous FULL; Git uses
`core.fsync=all` and `core.fsyncMethod=fsync`,
with packed imports retained (`fastimport.unpackLimit=1`). Each batch becomes visible at
its completion under the sole-writer assumption. This is not a crash-atomicity
or multi-ref transaction test. No simulated latency or model calls are involved.

The report retains raw wall times, CPU measurements, process counts, original
conditions and decisions, exact-state verification, and environment/settings.
Child CPU is available on POSIX; unavailable counters must be null. Repository
sizes are reported before and after separately timed maintenance, including
SQLite VACUUM and Git gc. Logical reachable tree bytes are a separate metric.

Verification checks the complete final path/content map, accepted history and
parent links, original base/condition audit data, and the one intentional stale
rejection. A mismatch invalidates a run, regardless of its speed.

## Reproduce

```sh
python -m unittest -v test_wide_tree_benchmark
python wide_tree_benchmark.py --widths 128 4096 32768 \
  --batches 1 8 32 --repeats 7 --proposals 32 \
  --output artifacts/wide-tree.json
```

The dedicated CI workflow preserves the raw report as an artifact. The ordinary
Linux/Windows test workflow checks the small correctness fixture.

A wider-directory speedup that also appears unchanged in the small and sharded
controls would not isolate directory width as its cause. Equal or faster Git
results must be reported. This local publisher experiment does not establish
remote API latency, multi-writer scalability, overall repository-size savings,
model productivity, or a general replacement for Git. Full-tree tests, searches,
exports, and Merkle proofs can shift work back onto Seed.

Git behavior and baseline capabilities are documented in the official
[commit-tree](https://git-scm.com/docs/git-commit-tree),
[fast-import](https://git-scm.com/docs/git-fast-import), and
[durability configuration](https://git-scm.com/docs/git-config#Documentation/git-config.txt-corefsync)
references.