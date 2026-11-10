# Seed

[![Tests](https://github.com/marvelousu/seed-vcs/actions/workflows/test.yml/badge.svg)](https://github.com/marvelousu/seed-vcs/actions/workflows/test.yml)

Seed is a version-control service for many coding agents that edit the same set
of files at the same time. An agent submits a **proposal**: *if the files I read
and the files I am replacing still have the contents I started with, apply these
replacements together.* The server either lands the whole proposal or refuses it
and names the paths that changed.

With Git, an agent that falls behind has to rebase or merge before it can
publish, even when nobody touched its files. In Seed, a proposal lands as it is
when unrelated changes went in first, and it is refused before publication when
something it read has changed.

[Is Seed for you?](#is-seed-for-you) · [Install](#requirements-and-install) ·
[Try it](#try-it-in-one-minute) · [Use it from an agent](#use-it-from-an-agent) ·
[What lands](#what-lands-and-what-is-refused) ·
[After a refusal](#when-a-proposal-is-refused) · [Limits](#limits) ·
[Documentation](#documentation)

## Is Seed for you?

Seed fits when:

- Several agents or scripts change one shared tree concurrently, mostly in
  different files, and rebuilding every change on the newest head costs you time.
- A change built on an outdated file should be stopped before it is published,
  not discovered after a merge.
- You still deliver through Git. The [Git bridge](docs/git-bridge.md) imports a
  snapshot and exports landed revisions to a dedicated branch.

Seed is the wrong tool when:

| You need | Use instead |
| --- | --- |
| Branches, pull requests and human review | Git with a forge. Seed has one head per repository and no review interface. |
| The server to merge concurrent edits to one file | Seed refuses the later proposal. `seed-agent rework --carry` merges in the client and submits a new proposal; see [After a refusal](#when-a-proposal-is-refused). |
| File modes, symlinks or submodules | Git. Seed stores regular file contents under logical paths. |
| Proof that the combined program works | Your tests. Landing checks file conditions only; [validation targets](docs/validation.md) record which revisions passed your checks. |
| A packaged, stable release | Seed is version 0.4, before 1.0, and is installed from a clone. It is not on PyPI. |

## Requirements and install

Python 3.12 or later. On systems that expose Python as `python3`, use that name.

```sh
git clone https://github.com/marvelousu/seed-vcs.git
cd seed-vcs
```

| To | Install |
| --- | --- |
| Run the demo, the local SQLite CLI, or `Store` | Nothing. They use the standard library only. |
| Use `seed-agent` or the Python client against a service | `python -m pip install --no-deps -e .` (standard library only) |
| Run the service | `python -m pip install -r requirements-server.txt`, then the line above |
| Run several service workers | PostgreSQL and an object volume shared by all workers; see [Running the service](docs/service.md) |

## Try it in one minute

```sh
python demo.py
```

The demo uses a temporary database and removes it afterwards.

```text
1. Two independent proposals from the same base:
   A: landed
   B, after HEAD advanced: landed
2. A stale edit of the same file:
   refused: a.py
   extra.py was not partially published
3. Different write path, but a declared read dependency changed:
   refused: api.py
4. The same dependency, left undeclared:
   proposal: landed
   actual program: NameError (semantic correctness still needs tests)
5. Retry an already landed proposal:
   A: already-landed
   no additional revision; all demo assertions passed
```

Step 4 is deliberate. Seed only knows about a dependency that was read through
it or declared. Agent sessions record their reads automatically, which is why
the next section uses them.

## Use it from an agent

Steps 1 to 3 and [After a refusal](#when-a-proposal-is-refused) were run in this
order against a fresh service, and the output shown is what they printed.
Revision and proposal IDs are shortened to eight characters, and yours will
differ.

### 1. Start a service and create a repository

```sh
python -m pip install -r requirements-server.txt
python -m pip install --no-deps -e .
seed-server token --repo demo
```

`token` prints a bearer credential on one line, once. Only its SHA-256 digest is
stored, in `.seed/credentials.json`. Put the credential in the `SEED_TOKEN`
environment variable of the terminal your agents run in:

```bash
export SEED_TOKEN='<token>'
```

```powershell
$env:SEED_TOKEN = '<token>'
```

Start the service in a second terminal, from the same directory. It listens on
`127.0.0.1:7331` and keeps repositories in SQLite files under `.seed/repos`.

```sh
seed-server serve
```

Create the repository. This call is idempotent.

```sh
python -c "import os; from seed_client import Client; print(Client('http://127.0.0.1:7331', 'demo', os.environ['SEED_TOKEN']).create())"
```

```text
{'revision': '4fc7b637…'}
```

### 2. Run one task from start to finish

Each task keeps its state in a JSON file, so an agent can stop and continue
later. `begin` pins the current revision.

```sh
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-init.json begin
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-init.json write config.txt --content greeting=Hello
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-init.json write notes.md --content "# Notes"
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-init.json propose --author you --message "Initial files"
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-init.json land
```

Every command prints one JSON object. The last two print:

```json
{"base":"4fc7b637…","proposal":"b7c83a4c…","status":"pending"}
{"revision":"0e98dc31…","status":"landed"}
```

`write` also accepts `--file PATH` and `--stdin`. `diff` shows the pending
edits, `status` shows the proposal's outcome, and `list` and `search` read the
tree. See the [agent workflow guide](docs/agent-workflow.md) for every command.

### 3. Let two agents work at once

Agent A reads `config.txt` and prepares a new file. Before A publishes, agent B
lands a change to a file A did not touch.

```sh
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-a.json begin
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-a.json read config.txt
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-a.json write banner.txt --content "Hello, world"

seed-agent --url http://127.0.0.1:7331 --repo demo --state task-b.json begin
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-b.json write notes.md --content "# Notes for agents"
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-b.json propose --author agent-b --message "Retitle notes"
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-b.json land

seed-agent --url http://127.0.0.1:7331 --repo demo --state task-a.json propose --author agent-a --message "Add banner"
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-a.json land
```

A's proposal still names the old base, and it lands without being rebuilt:

```json
{"base":"0e98dc31…","proposal":"69e8c9fb…","status":"pending"}
{"revision":"0aa0da47…","status":"landed"}
```

### 4. Run a command against the task's files

`run` writes the task's view of the repository into a directory, runs a command
there, and records what the command depended on. This example assumes a
repository with `src/` and `tests/`:

```sh
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-t.json run --workspace work --depends src --depends tests -- python -B -m unittest discover -s tests
```

The result contains the command's `returncode`, `stdout` and `stderr`, and
`"dependency_mode":"declared-dependencies"` with `"dependencies":["src","tests"]`.

- Without `--depends`, the whole materialized tree becomes a dependency, and a
  later change to any file in it refuses the proposal.
- With `--depends`, only the named subtrees and the files you edit are
  conditions. A command that reads other repository files needs them declared
  too.
- The command is an ordinary process (`"capture":"not-sandboxed"`). Seed records
  what you declare, not what the process opened.

## What lands and what is refused

| Since your task began | Result |
| --- | --- |
| Other files changed, none that you read or wrote | Lands, without rebuilding the proposal |
| A file you are replacing or deleting changed | Refused |
| A file you read changed, was created, or was deleted | Refused |
| A file was added to or removed from a directory you listed | Refused |
| Anything changed under a subtree you searched or ran a command against | Refused; narrow command dependencies with `run --depends` |
| A file changed and changed back to the same contents | Lands; conditions compare contents |
| One condition of a multi-file proposal fails | Refused as a whole; nothing is published |
| You land the same proposal again | Returns the original revision; history does not grow |
| A file your change depends on, but never read through Seed, changed | Lands. Seed cannot see that dependency, so test the combined result |

## When a proposal is refused

Agent C reads `config.txt` and writes `banner.txt`. Agent D then lands a change
to `config.txt`. C's `land` exits with code 0 and reports the conflict in JSON:

```json
{"head":"f4fabf33…","paths":{"config.txt":{"actual":"ae314970…","expected":"db95be2c…","kind":"content"}},"status":"conflict"}
```

Start a new task on the current head and carry the old edits over:

```sh
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-c.json rework --new-state task-c2.json --carry
```

The `carry` part of the result says what happened to each path:

```json
{"already_applied":[],"carried":["banner.txt"],"conflicts":[],"kept_ranges":[],"kept_reads":[],"merged":[],"stale_ranges":[],"stale_reads":["config.txt"]}
```

| Field | Meaning |
| --- | --- |
| `carried` | Your edit was reapplied; nobody else changed the file. |
| `merged` | Someone else changed the same file. Both sets of changes were combined line by line. |
| `conflicts` | The changes could not be combined. The reason is `overlapping-changes` (the changed regions touch or overlap), `add-add`, `delete-modify` or `binary`. Nothing was written for this path; redo the edit by hand. |
| `already_applied` | The head already contains your edit. |
| `kept_reads`, `kept_ranges` | Files and scopes you read that did not change. They stay conditions of the new task. |
| `stale_reads`, `stale_ranges` | Files and scopes you read that did change. Read them again before proposing. |

Seed does not let the new task claim a read it has not repeated:

```sh
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-c2.json propose --author agent-c --message "Greet everyone"
```

```json
{"message":"Carry has stale paths config.txt; re-read them (or re-run the command with seed-agent run) before proposing","status":"error"}
```

Read the changed file, adjust the edit if the new contents call for it, and
publish:

```sh
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-c2.json read config.txt
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-c2.json write banner.txt --content "Hi, everyone"
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-c2.json propose --author agent-c --message "Greet everyone"
seed-agent --url http://127.0.0.1:7331 --repo demo --state task-c2.json land
```

```json
{"revision":"74cd5fb9…","status":"landed"}
```

A merged file has not been tested in its merged form. Run your tests again with
`seed-agent run` before proposing.

## Limits

- **Sizes.** A request is at most 8 MiB. A proposal has at most 1,000 write
  paths, 1,000 read paths and 64 range conditions. A listed or searched scope
  holds at most 10,000 files; a larger scope fails instead of being truncated.
  A landing batch holds at most 256 proposals, and a listing page 1,000 items.
- **Conflicts are per file on the server.** Two edits to different lines of one
  file conflict at landing. Merging happens only in `rework --carry`.
- **Paths are logical names.** They are case-sensitive, relative POSIX paths. A
  file named `pkg` cannot coexist with `pkg/config`.
- **One ordered head per repository.** Publishers to the same repository are
  serialized. SQLite allows one writer per database; use PostgreSQL for several
  workers.
- **Author names are self-reported.** A content hash identifies bytes. It does
  not authenticate a contributor or stand for a review.
- **`seed-agent` exit codes.** `0` means the command ran, including a `land`
  that reports `"status":"conflict"`, so check the JSON. `1` means the command
  given to `run` failed or timed out. `2` means an error, reported as
  `{"status":"error","message":…}`.
- **Remote access needs HTTPS.** The client rejects remote HTTP unless you pass
  `--allow-http`. Put the service behind an HTTPS reverse proxy.

## Documentation

| Page | Read it when you want to |
| --- | --- |
| [Agent tasks and dependency conditions](docs/agent-workflow.md) | Use every `seed-agent` command, command workspaces, and rework |
| [Running the service](docs/service.md) | Configure the service, PostgreSQL, containers, and the Python client |
| [Local SQLite CLI and `Store`](docs/local-cli.md) | Use Seed in one process without a service |
| [Data model and landing rules](docs/model.md) | Understand blobs, proposals, revisions, and what landing checks |
| [Service architecture](docs/architecture.md) | See the protocol and transaction boundaries |
| [Validation and deployment targets](docs/validation.md) | Track which revisions passed your checks and which are deployable |
| [Git bridge](docs/git-bridge.md) | Import a Git snapshot or export revisions to a Git branch |
| [Backup and restore](docs/backup.md) | Back up a repository and verify the copy |
| [Development, tests and measurements](docs/development.md) | Run the tests, the load measurement, and the Git comparisons |

Recorded measurements: [HTTP load run](docs/benchmark.md),
[wide-directory publication](docs/wide-tree-benchmark.md),
[replayed agent traces](docs/agent-comparison.md), and the
[live coding study](docs/live-agent-study.md).

## Development

```sh
python -m unittest -v test_seed.py test_predicates.py test_backup.BackupTests test_git.py test_comparison.py test_workflow_environment.py test_merge.py
python demo.py
```

The service tests, the PostgreSQL contracts and the measurements are described
in [Development, tests and measurements](docs/development.md). CI runs the suite
on Ubuntu and Windows with Python 3.12.

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
