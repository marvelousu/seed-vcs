# Wide-directory publication experiment

Seed has a measurable possible use case when many small, already prepared
changes must retain individual revisions inside a wide generated directory.
Generated UI definitions, per-asset metadata, and generated configuration can
have this shape. This experiment measures the local publisher for that workload.

The comparison uses the existing SQLite Seed store and a conditional Git adapter
built on a bare repository and `git fast-import`. The Git adapter has no clone,
checkout, or staging index. It keeps a warm in-memory condition cache under a
sole-writer assumption and starts one importer for each nonempty accepted batch.
At batch size 1 this also means one process per accepted commit; at batch size 32
all 31 accepted commits use one process. Startup and mark-file I/O remain in its
reported publication time.

## Hypothesis and required history

Seed indexes current paths and per-path history. Git commits reference tree
objects that commit to a complete snapshot. Seed revisions retain reconstructible
file history, but do not provide Git's per-commit whole-tree Merkle commitment.
The hypothesis is that representing a wide directory at every historical step
adds work that Seed's indexed updates can avoid. The two engines provide the
required intermediate file states through different representations.

Every accepted proposal must retain its own revision, parent, and original
conditions. A service that only needs one Git commit for an entire batch can
reduce that requirement and may be a better design. Directory sharding is also
an available Git optimization, tested here as a control.

The predeclared matrix uses flat directories of 128, 4,096, and 32,768 files plus a
sharded control with the same 32,768 files and 128 siblings per leaf. Each layout
freezes 32 proposals at one base. A background revision changes an input read by
proposal 0, which must be rejected. The remaining 31 edit distinct existing paths
spread across the directory or shards and must be accepted. Batch sizes are 1,
8, and 32, with seven repeats and alternating engine order: 168 measured rows.
This is a publication workload with one stale-input check, rather than a
contention or conflict-rate experiment.

## Measurement boundary

Base creation, blob upload, immutable proposal preparation, repository copies,
background advance, verification, and maintenance happen outside the publication
window. Preparation and per-run setup times are retained separately. Copied
repository files are flushed before timing on POSIX; Windows records that this
setup barrier is unavailable. The timed work includes predicate
checks, publication, and Git importer startup/completion.

Seed uses WAL with synchronous FULL, checked through the Store connection
factory on each target. Git 2.43 or newer is required, with effective
`core.fsync=all`, `core.fsyncMethod=fsync`, `fastimport.unpackLimit=1`, and
`gc.auto=0` checked before timing. Packed imports preserve normal compression
and tree deltas and avoid launching `unpack-objects` for small imports. Git's
`fsyncMethod=batch` currently optimizes loose-object files; packed data still
uses the fsync path. The packed importer does not fsync each individual tree.
These settings request durable publication; this benchmark does not simulate a
power failure or establish identical filesystem crash guarantees.

Each batch becomes visible at its completion under the sole-writer assumption.
The adapter checks proposals sequentially, so a change accepted earlier in a
batch can invalidate a later member. Focused contracts compare write/write and
read-after-write decisions with Seed's `land_many`. Another contract verifies
that an interrupted importer missing its required `done` does not publish refs.

Verification checks the complete final path/content map, every accepted
historical payload, parent links, original condition audit data, the published
head, and the intentional rejection. Pending Git audit/payload objects remain
reachable after pruning. A mismatch invalidates a run regardless of speed.

Raw rows retain wall time, CPU accounting, process counts, storage figures, and
runtime/source metadata. POSIX CPU combines the parent and waited-for children;
Windows CPU accounting is unavailable. Physical repository sizes include normal
Git metadata files and are reported before and after separately timed Git gc or
SQLite VACUUM. Logical reachable tree bytes are separate from compressed storage
and network traffic. All cells, including unfavorable measurements, are retained.

## Reproduce

```sh
python -m unittest -v test_wide_tree_benchmark test_wide_tree_contracts
python wide_tree_benchmark.py --widths 128 4096 32768 \
  --batches 1 8 32 --repeats 7 --proposals 32 \
  --output artifacts/wide-tree.json
```

The dedicated CI workflow preserves the raw report as an artifact. The ordinary
Linux/Windows workflow runs the small correctness fixtures. Seven replays on one
runner describe that runner's variability; they are not seven independent
machines or agent-development sessions.

A width-dependent result must be assessed alongside the small and sharded
controls. This experiment supports a bounded local publication claim. Remote
API latency, concurrent writers, full-tree tests/search/export, overall model
productivity, and long-running Git service designs require their own measurements.

The baseline follows Git's official
[fast-import documentation](https://git-scm.com/docs/git-fast-import) and
[durability configuration](https://git-scm.com/docs/git-config#Documentation/git-config.txt-corefsync).
