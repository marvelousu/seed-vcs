# Seed

[![Tests](https://github.com/marvelousu/seed-vcs/actions/workflows/test.yml/badge.svg)](https://github.com/marvelousu/seed-vcs/actions/workflows/test.yml)

Seed is an experimental version-control kernel for applying **immutable,
conditional change proposals** to a shared file state. It uses content-addressed
storage and SQLite transactions, with no dependencies beyond Python's standard
library.

A proposal says: *if these files still contain the contents I started with,
apply these replacements together.* Unrelated changes can land in the meantime
without invalidating it. Changes to its write targets or declared dependencies
cause the whole proposal to be rejected.

The current implementation is a local CLI and Python API. Its interface and
database format are experimental; it has no Git interoperability or network
synchronization.

[Quick start](#quick-start) · [Usage](#usage) · [How it works](#how-it-works) ·
[Current scope](#current-scope) · [Contributing](#contributing)

## Features

- **Conditional writes.** Each edited path carries an expected content hash.
- **Explicit read dependencies.** Include additional paths whose contents your
  change relies on, even when you do not edit them.
- **Atomic landing.** File updates, history, and the head revision are published
  in one transaction, or rolled back together.
- **Idempotent retries.** Landing an accepted proposal again returns its original
  revision without adding another history entry.
- **Historical reads.** Retrieve previous file contents, including empty files
  and files that have since been deleted.
- **Concurrent local writers.** Separate processes can submit proposals to the
  same database; SQLite serializes their landing transactions.

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

## Current scope

Seed is a proof of the submission and landing model. It currently handles file
bytes in one local database. Working-tree checkout, text merging, branches,
remotes, permissions, and Git adapters are outside the implemented scope.

The following details matter when evaluating it:

- **Dependencies are explicit and file-based.** Undeclared dependencies, new
  entries in a directory listing, and external state are not tracked. Test and
  review the combined program separately.
- **Conflicts are conservative.** Different edits to the same file conflict,
  including edits to unrelated lines. Conditions compare current contents; a
  file that changed from A to B and back to A matches an A condition.
- **No-op writes are omitted.** Their implicit conditions are omitted too. Use
  `reads` to retain a condition on such a path. Proposals with no effective
  writes are rejected.
- **Paths are logical names.** Paths are case-sensitive, relative POSIX paths.
  A file named `pkg` cannot coexist with `pkg/config`. File modes, symlinks,
  submodules, and filesystem-specific export rules are not represented.
- **The storage model is local.** SQLite allows one writer at a time. Distributed
  scaling, power-loss recovery, and long-term production durability have not
  been evaluated. Use SQLite's backup API to obtain a consistent live backup;
  copying only the database file can miss pending WAL contents.

Author metadata is self-reported. Content hashes identify bytes; they do not
authenticate a contributor or represent a review approval.

## Development

Run the behavioral suite and demo from the repository directory:

```sh
python -m unittest -v test_seed.py
python demo.py
```

The suite covers independent proposals, rejected writes with no partial
publication, declared read conditions, idempotent retries, historical reads,
namespace collisions, storage-failure rollback, and multiple writer processes.
It also tests the limits of undeclared dependencies and directory listings.

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
