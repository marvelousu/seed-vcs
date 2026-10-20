# Service architecture

Agents submit immutable proposals to a repository service. The service checks
each proposal's expected file contents against the current state and atomically
publishes its replacements and revision. A failed proposal publishes nothing.

## Storage and concurrency

- PostgreSQL holds repository metadata, proposals, current paths and indexed
  per-path history. Each repository occupies a deterministic schema.
- File bytes live in an immutable, content-addressed object directory. Uploads
  are hashed and atomically installed before metadata can reference them.
  Service workers share this directory; separate hosts mount the same volume.
- Proposals are created without taking the repository head lock. Landing takes
  a row lock on that repository's head, then checks and applies changes. Other
  repositories and readers continue independently.
- Batch landing holds the head lock once and uses a savepoint per proposal.
  Successful proposals retain separate revisions; conflicts do not undo other
  successful proposals. The outer commit publishes the batch together.
- File and history listing uses bounded pages and cursors. Clients need not
  download a repository manifest or its full history to read or submit files.
- SQLite remains available for local use and development. Existing seed/1
  databases remain readable without a migration.

PostgreSQL read transactions use a repeatable snapshot. Landing uses read
committed isolation plus the head row lock, so a waiting writer checks the
state published by the preceding writer. All publishers must use this service
or its store API, rather than modifying its tables directly.

## Agent protocol

1. Pin a base revision once per task.
2. Read a work view of that revision plus pending edits. The Python session
   records requested paths, including absent files. Scoped listings record
   membership; literal searches record a conservative subtree condition.
3. Upload changed blobs and submit their IDs, paths and the captured read set.
4. Land the immutable proposal, alone or in a batch.
5. On conflict, start a new session at the current head and reconsider the
   affected code. A retry of an accepted proposal returns its original revision.

Automatic capture covers the session's read/list/search operations. Range
conditions are derived from the pinned base and checked in the same transaction
as file conditions. Historical range queries remain pinned even if another
publisher advances the head while a proposal is being created. Bounded scopes
fail above their cap rather than publishing an incomplete predicate.

The local `seed-agent` adapter persists tasks, exposes JSON commands and can
materialize an explicitly scoped regular-file workspace. Commands establish a
conservative subtree dependency; this is not transparent process-read tracing
or a sandbox. External services and machine state need separate handling.
The result of landing is a condition check, not a proof that tests will pass.
Task command results name their exact base and edited-state digest, and are not
treated as validation of arbitrary later combinations.

The task file is replaced atomically under a process-scoped OS advisory lock.
Proposal IDs are saved before landing, so an unknown success can be recovered
through a read-only status query. Rework creates a fresh task at the new head,
without reusing stale edits; its proposal names the superseded proposal.
Workspace manifests retain base blob IDs so repeated command runs replace the
whole scoped delta, including reversions. Their dependency guard is persisted
before command execution and restored after an interrupted first run.

Version 0.3 reads format-1 data. A format-2 proposal upgrades the repository's
format marker, which prevents old SQLite code from reopening it. A writer guard
also makes old PostgreSQL code reject format-2 proposals rather than ignore
their predicates. Stop all old writers before upgrading; old processes must
not be mixed with new workers. See the upgrade instructions in
[agent-workflow.md](agent-workflow.md).

## Access and operation

Bearer credentials are stored as SHA-256 digests in a server-side auth file.
Entries restrict repository names and read/write/create/validate/promote permissions. Every
repository endpoint checks its scope before invoking the store. The default
listener is loopback; remote clients use HTTPS through a TLS reverse proxy.

Request bodies, file counts, page sizes, batch sizes and connection pools have
limits. Each worker has a bounded database pool. Uploaded bytes are separate
from proposal metadata so retries do not need to resend file contents.

The logical backup command captures metadata and referenced objects at one pinned revision. Restore validates the archive before creating a fresh destination. See [backup.md](backup.md) for recovery and physical-backup requirements. Objects are not garbage-collected by the service; the PostgreSQL metadata schema is distinct from the SQLite file format.

## Validation

Run the same store contract against SQLite and PostgreSQL, plus HTTP access,
read capture, batch isolation, multi-process landing and retry tests. The
benchmark reports the repository size, clients, completed proposals, latency
percentiles, throughput, failures and environment. It measures this service;
it does not extrapolate an unmeasured advantage over Git.

## Candidate validation and delivery

Version 0.4 separates the integrated head from verified and deployable revisions.
A candidate previews a set of immutable proposals against one exact head, without
publishing them. Validation reports identify that candidate's final tree and a
required command/environment configuration. Publication takes the head lock only
after tests finish, and rejects a candidate if the starting head has changed.
A later failed report cannot be hidden by retrying an older successful report.
Changing the validation policy also invalidates eligibility under the old policy.

Validation and promotion have separate permissions from ordinary writes. Reports
are attestations by a trusted validator; the server does not prove that a remote
runner executed its command. Promotion selects an eligible exact revision and
records the decision; it does not deploy an application. See
[validation.md](validation.md) for the CLI and trust boundary.

The [Git bridge](git-bridge.md) imports a pinned tree or exports an exact Seed
revision to a dedicated Git branch with compare-and-swap ref updates. It does not
translate Seed observations into Git commit semantics. The
[agent comparison](agent-comparison.md) separates live coding traces from paired
replays, and reports transport and rework costs without treating replay counts as
independent model productivity samples.