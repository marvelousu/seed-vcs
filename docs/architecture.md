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
2. Read paths at that revision. The Python session records every requested
   path, including absent files, as a read condition.
3. Upload changed blobs and submit their IDs, paths and the captured read set.
4. Land the immutable proposal, alone or in a batch.
5. On conflict, start a new session at the current head and reconsider the
   affected code. A retry of an accepted proposal returns its original revision.

Automatic capture covers reads through the session API. Files read by shell
commands, directory enumeration and external services require separate handling.
The result of landing is a condition check, not a proof that tests will pass.

## Access and operation

Bearer credentials are stored as SHA-256 digests in a server-side auth file.
Entries restrict repository names and read/write/create permissions. Every
repository endpoint checks its scope before invoking the store. The default
listener is loopback; remote clients use HTTPS through a TLS reverse proxy.

Request bodies, file counts, page sizes, batch sizes and connection pools have
limits. Each worker has a bounded database pool. Uploaded bytes are separate
from proposal metadata so retries do not need to resend file contents.

Back up PostgreSQL and the object volume together. Retain all objects referenced
by the backup; objects are not garbage-collected by the service. The PostgreSQL
metadata schema is distinct from the SQLite file format.

## Validation

Run the same store contract against SQLite and PostgreSQL, plus HTTP access,
read capture, batch isolation, multi-process landing and retry tests. The
benchmark reports the repository size, clients, completed proposals, latency
percentiles, throughput, failures and environment. It measures this service;
it does not extrapolate an unmeasured advantage over Git.
