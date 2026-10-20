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

## Recorded result

The complete matrix passed on [GitHub Actions run 39369595597](https://github.com/marvelousu/seed-vcs/actions/runs/39369595597),
using source `c4d3364fdfdfad52a0a7b8fc1279ad2c83743ea9` with a clean checkout.
Environment: Ubuntu runner, Linux 6.17, four logical CPUs, Python 3.12.15,
SQLite 3.45.1, Git 2.55.0. CPU model and storage hardware were not identified;
this is one runner, not a hardware-normalized benchmark.

Every measured row accepted 31 proposals and rejected the stale-input proposal.
Both backends produced the same verified final content for every layout, with
individual historical payloads, parent chains, and retained proposal audit data.
The following are whole-run publication times for those 32 submissions, with
seven measurements per cell. IQR is the interquartile range, not a confidence
interval. Batch size includes the rejected proposal.

| Layout | Files | Batch | Seed median (IQR), ms | Git median (IQR), ms | Git / Seed |
| --- | ---: | ---: | ---: | ---: | ---: |
| flat | 128 | 1 | 56.71 (4.93) | 155.65 (6.33) | 2.7x |
| flat | 128 | 8 | 11.19 (3.85) | 25.76 (0.65) | 2.3x |
| flat | 128 | 32 | 5.77 (1.06) | 15.18 (0.62) | 2.6x |
| flat | 4,096 | 1 | 57.74 (2.25) | 386.77 (3.92) | 6.7x |
| flat | 4,096 | 8 | 13.20 (0.93) | 111.28 (1.95) | 8.4x |
| flat | 4,096 | 32 | 7.81 (0.31) | 78.32 (2.21) | 10.0x |
| flat | 32,768 | 1 | 57.70 (1.68) | 2148.37 (7.11) | 37.2x |
| flat | 32,768 | 8 | 13.83 (0.71) | 763.66 (2.19) | 55.2x |
| flat | 32,768 | 32 | 7.54 (0.35) | 580.71 (2.36) | 77.0x |
| sharded | 32,768 | 1 | 55.47 (3.07) | 164.01 (2.22) | 3.0x |
| sharded | 32,768 | 8 | 13.66 (0.76) | 39.03 (0.56) | 2.9x |
| sharded | 32,768 | 32 | 7.88 (0.21) | 25.21 (0.18) | 3.2x |

At batch size 32, Git uses one importer for all 31 commits. Flat 32,768 took
580.71 ms for Git and 7.54 ms for Seed (77.0x in this run).
Sharding those same files reduced Git to 25.21 ms (23.0x faster than
its flat layout), while Seed remained near 8 ms. The 128-file control has a
smaller fixed gap; the large additional gap tracks directory layout rather
than only importer startup. This supports the narrow wide-directory hypothesis
for this importer and these settings, without claiming an optimal Git service.

For batch size 32, the median CPU totals and complete repository sizes were:

| Layout / files | Seed CPU, ms | Git CPU, ms | Seed after VACUUM, MiB | Git after gc, MiB |
| --- | ---: | ---: | ---: | ---: |
| flat / 128 | 4.59 | 13.95 | 0.17 | 0.08 |
| flat / 4,096 | 5.97 | 77.29 | 2.34 | 0.39 |
| flat / 32,768 | 6.19 | 579.42 | 18.16 | 2.68 |
| sharded / 32,768 | 6.41 | 24.27 | 18.96 | 2.72 |

**Git uses substantially less storage here.** Seed's indexed SQLite representation
buys publication speed at a larger footprint in this fixture; Git compresses the
similar file contents and tree history effectively. Raw logical tree-byte counts
in the report are not compressed disk consumption or transferred bytes. Base
preparation, setup, flush, and maintenance timings remain available in the raw
report alongside publication time.

A concrete candidate use is an agent integration service accepting many prepared
per-asset/UI-definition updates, where each change needs its own reconstructible
history and a flat generated namespace is retained. For a normal source tree,
small isolated changes, or a workflow happy with a single batch commit, the
benefit needs separate evidence. Sharding directories is a practical Git option.
These measurements justify testing Seed in that specific workload; they do not
establish that an independent VCS is necessary for AI development in general.

The [complete raw report](measurements/wide-tree-20261020.json.gz) retains all 168
rows and original prepared conditions as gzip-compressed JSON. Decompress it
with Python's `gzip` module or any standard gzip tool. Its uncompressed SHA-256 is
`531d746991a8def5b3cc86f51bfd7a761bc84257920de5b3071b299cacec4b19`. The artifact and committed archive contain
identical JSON bytes; no slower cells were removed.

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
