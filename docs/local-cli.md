# Local SQLite CLI and Python `Store`

The original single-file store needs only Python's standard library and no
service. It is the quickest way to inspect the proposal model by hand. For
concurrent agents, use the [service](service.md) and the
[agent workflow](agent-workflow.md) instead.

Run commands from the repository directory. `--db` selects the SQLite database
and must appear before the subcommand; it defaults to `seed.db`.

## CLI

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

## Command reference

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

## Python API

The same operations are available through `Store` in [seed.py](../seed.py):

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
