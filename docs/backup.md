# Backup and restore

`seed_backup.py` creates a logical archive for one repository. It reads the
store through its read-snapshot transaction and copies its metadata together
with every referenced immutable blob. This includes historical file versions
and proposals that have been accepted but not yet landed. It is safe to back up
a live SQLite database without copying a `.db` file or WAL, and it gives the
PostgreSQL metadata and its object volume one consistent repository snapshot.

The archive is a new directory with a canonical `manifest.json` and
content-addressed files under `blobs/`. The manifest preserves all repository
metadata, including `candidate:*`, `validation:*`, `deployable`, and `verified`
records when present. It contains no HTTP credentials or server auth data.

Archives are trusted operational snapshots. Restoring one has database-admin
authority over a fresh destination. Verification establishes archive integrity
and self-consistency only: it does not authenticate the archive's origin or
give preserved workflow attestations and pointers validator provenance.
Restoring an archive does not install HTTP credentials or server authentication
configuration.

Create and verify an SQLite archive:

```sh
python seed_backup.py --db seed.db backup seed-backup-2026-10-20
python seed_backup.py verify seed-backup-2026-10-20
```

Restore it into a different, empty database path. If the target path does not
exist, the command initializes the fresh repository before restoring it.

```sh
python seed_backup.py --db restored.db restore seed-backup-2026-10-20
```

The command refuses a nonempty target and never replaces an existing archive
directory. It also refuses an archive with a missing, modified, oversized, or
unexpected blob file. Archive verification checks canonical JSON, proposal and
revision identities, the complete revision chain, every proposal base,
revision-bound writes against their version records, current-file
reconstruction, and every blob hash before a restore starts writing the
destination. It rejects symlinks and Windows reparse points (including
junctions) for the archive root, directories, and files; the archive tree is
inspected before any blob is opened.

For PostgreSQL, supply a dedicated object directory and repository name. The
database connection string stays in `SEED_DATABASE_URL`; it is not accepted as
a command argument and is never written to the archive or command output.

```sh
set SEED_DATABASE_URL=postgresql://...
python seed_backup.py --repository demo --objects /srv/seed-objects backup demo-backup
python seed_backup.py verify demo-backup
python seed_backup.py --repository demo-restored --objects /srv/seed-objects restore demo-backup
```

SQLite archives restore only to SQLite and PostgreSQL archives only to
PostgreSQL. A PostgreSQL restore can create a new repository schema, but still
rejects any repository that already contains work. Object bytes are immutable:
if a PostgreSQL restore fails after installing a blob but before its metadata
transaction commits, that unreferenced object may remain on disk while the
repository itself stays unchanged. It is safe to remove only through a future
object-reclamation procedure that proves the object is unreferenced.

Archives have explicit safety limits: at most 100,000 blobs, 1,000,000 metadata
records, 256 MiB per blob, 4 GiB total blob bytes, and 64 MiB for the manifest.
They are deliberately conservative operational artifacts, not a general-purpose
database dump format.
