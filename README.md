# Seed

[![Tests](https://github.com/marvelousu/seed-vcs/actions/workflows/test.yml/badge.svg)](https://github.com/marvelousu/seed-vcs/actions/workflows/test.yml)

Seed is a version-control service for concurrent agents. It applies **immutable,
conditional change proposals** to a shared file state, with a Python client,
an authenticated HTTP API, PostgreSQL metadata and content-addressed file objects.
The original SQLite CLI remains available using only Python's standard library.

A proposal says: *if these files still contain the contents I started with,
apply these replacements together.* Unrelated changes can land in the meantime
without invalidating it. Changes to its write targets or declared dependencies
cause the whole proposal to be rejected.

Agent sessions pin a revision and automatically capture files read through the
client API. Independent changes can land without rebuilding their proposals.
Multiple service workers share PostgreSQL and the object volume.

[Quick start](#quick-start) · [Usage](#usage) · [How it works](#how-it-works) ·
[Storage and behavior](#storage-and-behavior) · [Contributing](#contributing)

## Features

- **Conditional writes.** Each edited path carries an expected content hash.
- **Captured read dependencies.** Agent sessions record reads at their pinned
  revision, including missing files. Low-level APIs also accept explicit reads.
- **Working sessions.** Reads, listings and literal text searches include
  pending edits. Listings guard membership; searches guard the scoped subtree.
- **Restartable agent tasks.** A JSON CLI saves task state, proposals and
  outcomes, and supports explicitly scoped command workspaces.
- **Revision-bound validation.** Keep integrated, verified and deployable
  revisions separate; optionally validate a combined candidate before landing.
- **Git bridge and recovery.** Import a regular-file Git snapshot, export
  revisions to a dedicated Git branch, and verify logical repository backups.
- **Atomic landing.** File updates, history, and the head revision are published
  in one transaction, or rolled back together.
- **Idempotent retries.** Landing an accepted proposal again returns its original
  revision without adding another history entry.
- **Historical reads.** Retrieve previous file contents, including empty files
  and files that have since been deleted.
- **Concurrent service workers.** PostgreSQL coordinates publishers across
  processes; each repository has its own head lock and metadata schema.
- **Batch landing.** Accept multiple proposals in one transaction while keeping
  a separate result and revision for each successful proposal.
- **Repository-scoped credentials.** Restrict a bearer credential to named
  repositories and read, write, create, validate or promote permissions.
- **Bounded queries.** File and revision listings use cursor-based pages.

## Quick start

Requires **Python 3.12 or later**. Clone the repository and run the demonstration:

```sh
git clone https://github.com/marvelousu/seed-vcs.git
cd seed-vcs
python demo.py
```

On systems where Python is exposed as `python3`, use that command instead.
There is no package installation or build step.

The demo creates an isolated temporary database and exercises independent
changes, conflicting writes, declared read dependencies, and retries. It also
shows an accepted change that breaks a program because a dependency was not
declared. Successful landing checks file conditions; it does not establish
program correctness.

## Run the service

Install the service and create a credential for a repository:

```sh
python -m pip install -r requirements-server.txt
python -m pip install --no-deps -e .
seed-server token --repo demo
seed-server serve
```

The `token` command prints the bearer credential once and adds its SHA-256
digest to `.seed/credentials.json`. Keep the credential in your secret manager;
the service reads only the digest file. Restart workers after updating grants.
The default listener is `127.0.0.1:7331`. Without `SEED_DATABASE_URL`, repositories
use SQLite files under `.seed/repos`.

For PostgreSQL, supply `SEED_DATABASE_URL` through your secret manager or process
configuration, then start multiple workers:

```sh
seed-server serve --workers 4
```

The PostgreSQL account must be able to create schemas in the selected database.
Workers use PostgreSQL for metadata and `.seed/objects` for file bytes. All
workers and hosts serving the same repositories must share that object volume.
Serve remote connections through an HTTPS reverse proxy; remote HTTP is rejected
by the client unless explicitly enabled.

| Configuration | Default | Purpose |
| --- | --- | --- |
| `SEED_DATA` | `.seed` | Object directory and local SQLite repositories. |
| `SEED_AUTH_FILE` | `$SEED_DATA/credentials.json` | Repository grants and credential digests. |
| `SEED_DATABASE_URL` | unset | PostgreSQL connection string; unset selects SQLite. |
| `SEED_POOL_SIZE` | `16` | Maximum PostgreSQL connections per worker. |

Requests are limited to 8 MiB, proposals to 1,000 write paths and 1,000 read
paths, landing batches to 256 proposals, and listing pages to 1,000 items.
The server also limits concurrent connections and queued database acquisitions.

### Agent client

The client uses Python's standard library. Pass your credential from the
environment populated by your secret manager:

```python
import os
from seed_client import Client

client = Client("http://127.0.0.1:7331", "demo", os.environ["SEED_TOKEN"])
client.create()  # Idempotent repository creation; requires create permission.

task = client.begin()
previous = task.read("greeting.txt")  # Read condition is captured automatically.
task.write("greeting.txt", b"Hello from an agent!\n")
proposal = task.propose(author="agent-1", message="Update greeting")
print(client.land(proposal))
```

In version 0.3, `task.read()` reads the pinned base **plus pending edits**.
Use `task.read_base()` to read the original pinned version explicitly. An absent
file is captured as an absence condition. `task.write(path, None)` proposes
deletion. `task.files("src/")` records directory membership, while
`task.search("TODO", "src/")` guards all paths and contents in that subtree,
including files which did not match. Scopes are bounded to 10,000 files and
proposals to 64 range conditions; oversized scopes fail rather than truncate
their dependency conditions. Search output is bounded separately.

The [agent workflow guide](docs/agent-workflow.md) covers the persistent
`seed-agent` CLI, command workspaces, safe rework and the version 0.3 upgrade.

Version 0.4 adds [validation and deployment targets](docs/validation.md),
the [one-way Git bridge](docs/git-bridge.md), and [backup/restore](docs/backup.md).
The [agent comparison guide](docs/agent-comparison.md) explains how captured
coding traces are replayed against Seed and automated Git workflows.
The [wide-directory publication experiment](docs/wide-tree-benchmark.md) tests
local integration cost against a batched bare-Git importer, with small and
sharded controls.

`Client.land_many(ids)` returns one result per proposal. Conflicts and namespace
errors do not undo other successful proposals; a database failure rolls back
the whole batch. HTTP 409 reports changed conditions. Temporary connection
errors and HTTP 429/503 are retried up to three times. Replaying a successful
landing returns `already-landed` and the original revision.

### Containers

The included Compose configuration starts PostgreSQL and four service workers.
Create `.seed/credentials.json` with the token command first, supply
`POSTGRES_PASSWORD` through your secret manager, and run:

```sh
docker compose up --build -d
```

The port binds to loopback. Metadata and objects use persistent volumes. The
application runs as a non-root user. `/healthz` checks the process and `/readyz`
checks database availability. Back up PostgreSQL and its referenced objects
together. Uploaded objects are retained; the service does not delete them.

## Usage

Run commands from the repository directory. `--db` selects the SQLite database
and must appear before the subcommand; it defaults to `seed.db`.

### CLI

This example creates a database, submits a file, and reads it back. Use an unused
database path: `init` refuses to overwrite an existing file.

**Bash**

```bash
python seed.py --db example.db init
python -c "from pathlib import Path; Path('payload.txt').write_bytes(b'Hello, Seed!\n')"
base=$(python seed.py --db example.db head)
proposal=$(python seed.py --db example.db propose --base "$base" --set hello.txt=payload.txt --author alice --message "Add a greeting")
python seed.py --db example.db show "$proposal"
python seed.py --db example.db land "$proposal"
python seed.py --db example.db cat hello.txt
```

**PowerShell**

```powershell
python seed.py --db example.db init
python -c "from pathlib import Path; Path('payload.txt').write_bytes(b'Hello, Seed!\n')"
$base = python seed.py --db example.db head
$proposal = python seed.py --db example.db propose --base $base --set hello.txt=payload.txt --author alice --message "Add a greeting"
python seed.py --db example.db show $proposal
python seed.py --db example.db land $proposal
python seed.py --db example.db cat hello.txt
```

The final command prints `Hello, Seed!`. Creating a proposal does not update the
current file state; `land` does.

`--set REPO_PATH=LOCAL_FILE` reads bytes from a local file and stores them under a
logical repository path. Repeat `--set` to submit several files together. Use
`--delete REPO_PATH` for deletions and `--read REPO_PATH` for additional read
conditions. A rename is a deletion and an addition in the same proposal.

Read conditions are captured from `--base`. Read dependencies from that revision
when preparing a change; listing a path with `--read` does not automatically
observe files read by an editor or another process.

### Command reference

| Command | Purpose |
| --- | --- |
| `init` | Create an empty database and its initial revision. |
| `head` | Print the current revision ID. |
| `propose --base REV --set PATH=FILE` | Store a proposal; print its ID. |
| `show PROPOSAL` | Print a proposal's conditions, writes, and metadata as JSON. |
| `land PROPOSAL` | Apply the proposal or report a conflict. |
| `ls [--rev REV]` | Print a JSON map of paths to content IDs. |
| `cat PATH [--rev REV]` | Write the file's raw bytes to standard output. |
| `log` | Print accepted revisions in chronological order as JSON. |

`land` reports `landed` or `already-landed` with the resulting revision ID.
Conditions that no longer match are reported as JSON on standard error, with
the expected and actual content IDs for each conflicting path.

| Exit code | Meaning |
| --- | --- |
| `0` | The command succeeded. |
| `1` | A storage, input, or namespace error occurred. |
| `2` | Proposal conditions conflicted, or command-line parsing failed. |

### Python API

The same operations are available through `Store` in [seed.py](seed.py):

```python
from seed import Store

store = Store.create("api-example.db")
base = store.head()

first = store.propose(base, {"a.txt": b"A\n"}, author="agent-a")
second = store.propose(base, {"b.txt": b"B\n"}, author="agent-b")

store.land(first)
store.land(second)  # Still valid: b.txt has not changed since base.

assert store.read("a.txt") == b"A\n"
assert store.read("b.txt") == b"B\n"
assert store.read("a.txt", base) is None
```

Use `Store("existing.db")` to open a database. `propose` accepts a mapping of
paths to `bytes` or `None`, with `None` representing deletion. Its `reads`
argument declares additional dependency paths. `land` raises `Conflict` when
conditions differ; `Conflict.differences` contains the conflicting paths.

## How it works

Seed combines content-addressed objects with optimistic concurrency control.
Its model has three parts:

| Object | Contents |
| --- | --- |
| Blob | Immutable file bytes, identified by a type-prefixed SHA-256 hash. |
| Proposal | A base revision, expected file hashes, desired replacements, and author/message metadata, encoded as canonical JSON. |
| Revision | The previous head and the accepted proposal ID. |

Write conditions are inferred from the proposal's base revision. Declared read
conditions are added to the same set. `null` means the file is absent, which is
different from the hash of an empty file.

Landing takes a SQLite write transaction, checks for a previous acceptance,
compares all conditions against the current file state, and applies all writes.
It validates the resulting file namespace, records the revision and per-path
history, then advances the head. Any failure rolls back the transaction.

The proposal remains unchanged when the head advances. The new revision's
parent is the actual head at landing time, rather than the proposal's original
base. Two independent proposals can produce the same final file state in either
order, while producing different revision IDs and history.

The database maintains a current path table and per-path version records,
including deletion records. Landing records only the edited paths rather than
copying a full repository snapshot. Historical reads use those version records
to reconstruct file state at a revision.

## Storage and behavior

Both storage backends use the same proposal and revision identities and landing
rules. SQLite stores file bytes in its database. PostgreSQL stores blob IDs and
sizes while the object volume holds the immutable bytes; reads verify their
content hashes. Proposal creation and uploads do not take the repository head
lock. Landing locks that repository's head, checks conditions and publishes the
revision. Batch landing amortizes lock acquisition and durable commit overhead.

[Architecture](docs/architecture.md) describes the protocol and transaction
boundaries. The local task adapter materializes scoped regular-file workspaces;
the core service publishes file states. The Git bridge handles explicit one-way
snapshot import and revision export. Line-based merging and simultaneous
two-way synchronization remain separate design work.

The following details matter when evaluating it:

- **Dependencies follow the operations used.** Session reads capture file
  content and absence; `files` captures scoped membership; `search` captures a
  conservative subtree condition. Arbitrary shell and external inputs need
  explicit handling. Test and review the combined program separately.
- **Conflicts are conservative.** Different edits to the same file conflict,
  including edits to unrelated lines. Conditions compare current contents; a
  file that changed from A to B and back to A matches an A condition.
- **No-op writes are omitted.** Their implicit conditions are omitted too. Use
  `reads` to retain a condition on such a path. Proposals with no effective
  writes are rejected.
- **Paths are logical names.** Paths are case-sensitive, relative POSIX paths.
  A file named `pkg` cannot coexist with `pkg/config`. File modes, symlinks,
  submodules, and filesystem-specific export rules are not represented.
- **Publishers have a single ordered head per repository.** PostgreSQL readers
  use repeatable snapshots; publishers serialize on that head. Separate
  repositories proceed independently. SQLite allows one writer per database.
  Use SQLite's backup API to obtain a consistent live backup;
  copying only the database file can miss pending WAL contents.

Author metadata is self-reported. Content hashes identify bytes; they do not
authenticate a contributor or represent a review approval.

## Development

Run the behavioral suite and demo from the repository directory:

```sh
python -m unittest -v test_seed.py test_predicates.py test_backup.BackupTests test_git.py test_comparison.py test_workflow_environment.py
python demo.py
```

Install service test dependencies and run the HTTP contracts:

```sh
python -m pip install httpx==0.28.1
python -m unittest -v test_service.ServiceTests test_session.py test_agent.py test_validation.ValidationTests
```

Set `SEED_TEST_DATABASE_URL` to a **dedicated test database** and run
`python -m unittest -v test_service.PostgresServiceTests test_validation.PostgresValidationTests test_backup.PostgresBackupTests`
to exercise the HTTP, validation and restore contracts on PostgreSQL. Tests
create and remove their own random schemas.
CI runs SQLite/core and service tests on Windows and Linux, PostgreSQL contracts
on Linux, and builds the container image.

### Load measurement

Use an empty repository with an authorized credential to measure the real HTTP
service. Load an initial repository, submit independent changes from concurrent
clients, then verify all file paths and revision sequence numbers:

```sh
seed-benchmark --repo bench --files 100000 --proposals 1000 --clients 64 --batch 8 --output result.json
```

The command reads `SEED_TOKEN`. Results include completed proposals, throughput,
batch latency percentiles, failures and the client environment. It measures
proposal creation and landing over HTTP, including blob uploads. Initial
repository loading and final verification are timed separately from submissions.

A [recorded run](docs/benchmark.md) used 100,000 initial paths, four HTTP workers
and 64 concurrent clients: 1,000 proposals landed in 18.41 seconds with no errors.
The report includes the payload sizes, initial load time and latency percentiles.

The suite covers independent proposals, rejected writes with no partial
publication, declared read conditions, idempotent retries, historical reads,
namespace collisions, storage-failure rollback, and multiple writer processes.
It also covers scoped dependency conditions, work-view semantics, persistent
task recovery and command workspace safety. Legacy low-level calls still
demonstrate what happens when a dependency is not declared.

For a deterministic comparison with automated Git and Git with equivalent
dependency checks, run `python workflow_benchmark.py --output artifacts/workflow-replay.json`.
See the [workflow guide](docs/agent-workflow.md#measure-before-extending-the-architecture)
for what this replay measures and the real-agent measurements still needed.

[GitHub Actions](https://github.com/marvelousu/seed-vcs/actions/workflows/test.yml)
runs the suite and demo on Ubuntu and Windows with Python 3.12. Tests and the
demo use temporary databases under `artifacts/tmp` and clean them up on exit.

## Contributing

Bug reports and focused pull requests are welcome. For a bug report, include
the Python version, operating system, a minimal sequence of commands, and the
expected and actual result. Use small, synthetic file contents for reproductions
rather than uploading a database containing private code.

For changes to landing or history behavior, include a regression test and
describe any effects on existing databases or proposal identity. Please keep
the core dependency-free and run the commands in [Development](#development)
before submitting a pull request.

## Related work

Seed uses established ideas from content-addressed storage and optimistic
concurrency control. Background reading:

- [Git's object model](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects)
  explains content-addressed blobs, trees, and commits.
- [Anthropic's Scion design](https://www.anthropic.com/engineering/redesigning-source-control)
  describes source-control workloads at agent scale and motivated this experiment.
- [Gerrit's Change-Ids](https://gerrit-review.googlesource.com/Documentation/user-changeid.html)
  provide another example of change identity separated from commit identity.

## License

Seed is released under the [MIT License](LICENSE).
