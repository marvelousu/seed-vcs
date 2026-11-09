# Data model and landing rules

This page explains what a proposal is, what the server checks when it lands
one, and the behavior that follows from those rules.
[Architecture](architecture.md) describes the protocol and transaction
boundaries in more detail.

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

The local task adapter materializes scoped regular-file workspaces;
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
