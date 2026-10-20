# Agent tasks and dependency conditions

Seed 0.3 adds a working view, scoped conditions and a restartable JSON CLI.
The server remains the publisher. Successful landing means that the recorded
conditions passed, not that the integrated program passed its tests.

## Upgrade

Stop all old service workers before starting 0.3 workers against the same data.
The new code reads existing repositories and format-1 proposals. Ordinary
proposals without ranges or lineage keep their original format and IDs.
The first format-2 proposal upgrades the repository's metadata format marker.
Old SQLite code cannot reopen that repository. Format-2 proposals also include
a fail-closed writer guard, so old PostgreSQL code which did not inspect the
metadata marker cannot silently land them while ignoring their predicates.
The client reports this old-writer rejection as a protocol error requiring a
worker upgrade, rather than suggesting that the task should be reworked.
Mixing old and new writers remains unsupported. Back up
metadata and the object volume before updating a repository that matters.

`Session.read()` now reads the pinned base plus pending edits. Call
`read_base()` when the unmodified starting version is required. Creation,
modification and deletion are immediately reflected in read, list and search.

## Captured inputs

| Operation | Landing condition |
| --- | --- |
| `read(path)` / `read_base(path)` | The base content, or absence, of that path |
| `files(prefix)` | The complete set of base paths under that directory |
| `search(query, prefix)` | All base paths and contents under that directory |
| Scoped command workspace | All base paths and contents in the declared scope |

Scope `src` and `src/` both mean descendants of `src/`; an empty prefix means
the repository root. Names such as `src-other/` are outside `src/`.
These initial range predicates are conservative. An unrelated content change
inside a searched subtree can cause rejection. A pure membership observation
does not reject a content-only change, but reading that file adds its content
condition. Overlay files are visible in the work view; their external conditions
are always derived from the pinned base, not from invented base contents.

Each range covers at most 10,000 files and fails explicitly above the cap. A
proposal may contain 64 ranges, 1,000 explicit reads and 1,000 changed paths.
Search is literal, case-sensitive UTF-8 line search; binary files are skipped.
The output limit sets `truncated`, while the entire scope remains guarded.
All predicates are checked under the publisher's existing transaction/head lock.

## Persistent CLI

Install the project and start a service as described in the README. Create the
repository with `Client.create()` and provide its credential as `SEED_TOKEN`
from your secret manager. The credential is not saved in task state.

The following commands use one task state across separate processes:

```sh
seed-agent --url http://127.0.0.1:7331 --repo demo --state artifacts/task.json begin
seed-agent --url http://127.0.0.1:7331 --repo demo --state artifacts/task.json search TODO --prefix src/
seed-agent --url http://127.0.0.1:7331 --repo demo --state artifacts/task.json read src/example.py
seed-agent --url http://127.0.0.1:7331 --repo demo --state artifacts/task.json write src/example.py --file revised-example.py
seed-agent --url http://127.0.0.1:7331 --repo demo --state artifacts/task.json diff
seed-agent --url http://127.0.0.1:7331 --repo demo --state artifacts/task.json propose --message "Update example"
seed-agent --url http://127.0.0.1:7331 --repo demo --state artifacts/task.json land
seed-agent --url http://127.0.0.1:7331 --repo demo --state artifacts/task.json status
```

Responses are JSON. File bytes use explicit UTF-8 or Base64 encoding. Task state
contains local source edits and should be kept private even though it contains
no bearer credential. Use separate state paths for separate tasks. A task is
bound to the original URL, repository and base revision.

Once a proposal is frozen, use status/describe/diff to inspect it. If a landing
response is lost, status recovers the accepted revision; replaying land remains
idempotent. Changing a frozen task requires a new task:

```sh
seed-agent --url http://127.0.0.1:7331 --repo demo --state artifacts/task.json rework --new-state artifacts/rework.json
```

Rework starts at the current head and links the eventual new proposal to the old
one through `supersedes`. It deliberately starts with no old edits or reads.
Read the changed inputs and recreate the appropriate output; merely replacing
old expected hashes would bypass the conflict which was just detected.

## Command workspace

`run` creates a new local directory from one declared subtree, executes a command
with `shell=False`, and collects regular-file additions, edits and deletions:

```sh
seed-agent --url http://127.0.0.1:7331 --repo demo --state artifacts/task.json run --workspace artifacts/task-work --prefix src/ -- python -B -m unittest
```

The root of this workspace corresponds to the selected prefix. Select the root
scope when the project needs files from multiple top-level directories. Existing
directories are not overwritten. Unsupported path names, symlinks/reparse points
and case collisions fail before they can be silently exported as different data.
If a command creates an unsupported link, remove that entry with a repair
command in the same workspace, then run collection again. The workspace remains
available for inspection after an error.

`run` emits its JSON result and exits with status 1 when the command fails or
times out. Partial edits remain available for inspection and correction; inspect
`diff` and rerun the command before proposing them. A successful test command
describes that local candidate, not the state of a later integrated revision.

This is an explicitly scoped local runner, **not a sandbox or transparent read
tracer**. Commands can use machine files and network resources outside the scope;
those inputs are not covered by a repository subtree condition. The server never
executes the command. `SEED_TOKEN` is removed from the child environment.
Use the runner for code you are willing to execute with your local privileges.
Its result records the base and edited-state digest, return code and elapsed
time. It does not label a later combined head as tested.

## Measure before extending the architecture

```sh
seed-workflow-benchmark --repeats 3 --output artifacts/workflow-replay.json
```

This replays six known scenarios against native Seed, actual Git worktrees with
automatic merge, and Git with equivalent conditional checks. The safety oracle
is separate from the fixture's observed read set, including an incidental read
which deliberately causes a conservative rejection. The report shows accepted
changes, unsafe acceptance, conservative rejection and local elapsed time.

It is a deterministic local replay, not a real-agent productivity benchmark:
the read sets are fixture observations, model/token costs are absent, and the
local implementation/setup costs differ. Use actual task traces and bounded
paired agent runs before making claims about productivity or native-store speed.

`Client.metrics` reports request count, uploaded/downloaded body bytes and request
time. The verified blob cache is local to that client instance and bounded to
128 entries and 16 MiB. Separate CLI processes do not share that in-memory cache.
