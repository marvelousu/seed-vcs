"""Seed: conditional change packages, not a production Git replacement.

Python 3.12+, standard library only. Logical POSIX file paths; no checkout.
The SQLite transaction is the only publication boundary.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Iterable, Mapping


class SeedError(Exception):
    pass


class Conflict(SeedError):
    def __init__(self, differences: dict):
        self.differences = differences
        super().__init__("Conditions changed: " + ", ".join(differences))


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def object_id(kind: str, payload: bytes) -> str:
    # Domain separation makes blob/proposal/revision distinct object types.
    return hashlib.sha256(kind.encode("ascii") + b"\0" + payload).hexdigest()


def file_path(path: str) -> str:
    if (not isinstance(path, str) or not path or "\\" in path or "\0" in path
            or any(part in ("", ".", "..") for part in path.split("/"))):
        raise SeedError(f"Not a canonical relative file path: {path!r}")
    return path


SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE blobs (id TEXT PRIMARY KEY, payload BLOB NOT NULL);
CREATE TABLE proposals (id TEXT PRIMARY KEY, payload BLOB NOT NULL);
CREATE TABLE revisions (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    parent TEXT REFERENCES revisions(id),
    proposal TEXT UNIQUE REFERENCES proposals(id)
);
CREATE TABLE current_files (
    path TEXT PRIMARY KEY,
    blob TEXT NOT NULL REFERENCES blobs(id)
);
CREATE TABLE versions (
    path TEXT NOT NULL,
    seq INTEGER NOT NULL REFERENCES revisions(seq),
    blob TEXT REFERENCES blobs(id),
    PRIMARY KEY (path, seq)
);
"""


class Store:
    def __init__(self, database: str | Path):
        self.database = Path(database).resolve()
        if not self.database.is_file():
            raise SeedError("Database does not exist; run init first")
        try:
            with self._db() as db:
                row = db.execute("SELECT value FROM meta WHERE key='format'").fetchone()
                if row is None or row[0] != "seed/1":
                    raise SeedError("Unsupported database format")
        except sqlite3.DatabaseError as exc:
            raise SeedError("Not a Seed database") from exc

    @classmethod
    def create(cls, database: str | Path) -> Store:
        destination = Path(database).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination.touch(exist_ok=False)
        except FileExistsError as exc:
            raise SeedError("init refuses to overwrite an existing file") from exc
        db = sqlite3.connect(destination, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            root = object_id("revision", canonical({"parent": None, "proposal": None}))
            db.execute("INSERT INTO revisions VALUES (0, ?, NULL, NULL)", (root,))
            db.executemany("INSERT INTO meta VALUES (?, ?)",
                           [("format", "seed/1"), ("head", root)])
            db.commit()
        finally:
            db.close()
        return cls(destination)

    @contextmanager
    def _db(self, write: bool = False):
        db = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _head(db) -> str:
        return db.execute("SELECT value FROM meta WHERE key='head'").fetchone()[0]

    @staticmethod
    def _revision(db, revision: str):
        row = db.execute("SELECT * FROM revisions WHERE id=?", (revision,)).fetchone()
        if row is None:
            raise SeedError("Unknown revision: " + revision)
        return row

    @staticmethod
    def _at(db, path: str, seq: int) -> str | None:
        row = db.execute("SELECT blob FROM versions WHERE path=? AND seq<=? "
                         "ORDER BY seq DESC LIMIT 1", (path, seq)).fetchone()
        # A tombstone is a result, not an invitation to search further back.
        return None if row is None else row[0]

    @staticmethod
    def _current(db, path: str) -> str | None:
        row = db.execute("SELECT blob FROM current_files WHERE path=?", (path,)).fetchone()
        return None if row is None else row[0]

    @staticmethod
    def _blob(db, identifier: str) -> bytes:
        row = db.execute("SELECT payload FROM blobs WHERE id=?", (identifier,)).fetchone()
        if row is None or object_id("blob", row[0]) != identifier:
            raise SeedError("Missing or damaged blob: " + identifier)
        return row[0]

    @staticmethod
    def _proposal(db, identifier: str) -> dict:
        row = db.execute("SELECT payload FROM proposals WHERE id=?", (identifier,)).fetchone()
        if row is None or object_id("proposal", row[0]) != identifier:
            raise SeedError("Missing or damaged proposal: " + identifier)
        return json.loads(row[0])

    def head(self) -> str:
        with self._db() as db:
            return self._head(db)

    def propose(self, base: str, edits: Mapping[str, bytes | None], *,
                reads: Iterable[str] = (), author: str = "", message: str = "") -> str:
        """Freeze file predicates at base. No-op edits are omitted entirely."""
        if isinstance(reads, str):
            raise SeedError("reads must be a collection of paths, not a string")
        read_paths = {file_path(path) for path in reads}
        if not isinstance(author, str) or not isinstance(message, str):
            raise SeedError("author and message must be strings")
        with self._db(write=True) as db:
            writes = {}
            for path, payload in sorted(edits.items()):
                file_path(path)
                if payload is not None and not isinstance(payload, bytes):
                    raise SeedError("File contents must be bytes or None")
                writes[path] = None if payload is None else self._put_blob(db, payload)
            return self._freeze(db, base, writes, read_paths, author, message)

    def _put_blob(self, db, payload: bytes) -> str:
        identifier = object_id("blob", payload)
        db.execute("INSERT OR IGNORE INTO blobs VALUES (?, ?)", (identifier, payload))
        return identifier

    def put_blob(self, payload: bytes) -> str:
        if not isinstance(payload, bytes):
            raise SeedError("File contents must be bytes")
        with self._db(write=True) as db:
            return self._put_blob(db, payload)

    def get_blob(self, identifier: str) -> bytes:
        with self._db() as db:
            return self._blob(db, identifier)

    def _verify_blob(self, db, identifier: str):
        self._blob(db, identifier)

    def _verify_blobs(self, db, identifiers):
        for identifier in set(identifiers):
            self._verify_blob(db, identifier)

    def propose_refs(self, base: str, writes: Mapping[str, str | None], *,
                     reads: Iterable[str] = (), author: str = "", message: str = "") -> str:
        """Submit previously uploaded blobs without transferring their bytes again."""
        if isinstance(reads, str):
            raise SeedError("reads must be a collection of paths")
        with self._db(write=True) as db:
            for path, blob in writes.items():
                file_path(path)
            self._verify_blobs(db, (b for b in writes.values() if b is not None))
            return self._freeze(db, base, writes, {file_path(p) for p in reads}, author, message)

    def _freeze(self, db, base, writes, read_paths, author, message):
        if not isinstance(author, str) or not isinstance(message, str):
            raise SeedError("author and message must be strings")
        seq = self._revision(db, base)["seq"]
        expected = self._at_many(db, set(writes) | read_paths, seq)
        writes = {p: b for p, b in writes.items() if b != expected[p]}
        expected = {p: b for p, b in expected.items() if p in writes or p in read_paths}
        if not writes:
            raise SeedError("No effective writes; conditions-only proposals are unsupported")
        body = canonical({"format": 1, "base": base, "expected": expected,
                          "writes": writes, "author": author, "message": message})
        identifier = object_id("proposal", body)
        db.execute("INSERT OR IGNORE INTO proposals VALUES (?, ?)", (identifier, body))
        return identifier

    @classmethod
    def _at_many(cls, db, paths, seq):
        paths = sorted(paths)
        result = dict.fromkeys(paths)
        for start in range(0, len(paths), 400):
            chunk = paths[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = db.execute(
                "SELECT path, blob FROM (SELECT path, blob, "
                "ROW_NUMBER() OVER (PARTITION BY path ORDER BY seq DESC) AS rank "
                f"FROM versions WHERE seq<=? AND path IN ({placeholders})) latest WHERE rank=1",
                (seq, *chunk))
            result.update((row["path"], row["blob"]) for row in rows)
        return result

    @classmethod
    def _current_many(cls, db, paths):
        paths = sorted(paths)
        result = dict.fromkeys(paths)
        for start in range(0, len(paths), 400):
            chunk = paths[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = db.execute(f"SELECT path, blob FROM current_files WHERE path IN ({placeholders})",
                              chunk)
            result.update((row["path"], row["blob"]) for row in rows)
        return result

    def _lock_head(self, db):
        # SQLite's BEGIN IMMEDIATE already serializes publishers.
        pass

    def land(self, proposal: str) -> dict:
        """Check and publish the entire proposal, or change nothing."""
        with self._db(write=True) as db:
            self._lock_head(db)
            return self._land(db, proposal)

    def land_many(self, proposals: Iterable[str]) -> list[dict]:
        proposals = list(proposals)
        if len(proposals) > 256:
            raise SeedError("At most 256 proposals per batch")
        with self._db(write=True) as db:
            self._lock_head(db)
            results = []
            for proposal in proposals:
                db.execute("SAVEPOINT proposal_landing")
                try:
                    result = self._land(db, proposal)
                except (Conflict, SeedError) as exc:
                    db.execute("ROLLBACK TO SAVEPOINT proposal_landing")
                    result = ({"status": "conflict", "paths": exc.differences}
                              if isinstance(exc, Conflict) else
                              {"status": "error", "message": str(exc)})
                db.execute("RELEASE SAVEPOINT proposal_landing")
                results.append({"proposal": proposal, **result})
            return results

    def _land(self, db, proposal):
        body = self._proposal(db, proposal)
        landed = db.execute("SELECT id FROM revisions WHERE proposal=?", (proposal,)).fetchone()
        if landed:
            return {"revision": landed[0], "status": "already-landed"}
        parent = self._head(db)
        differences = {}
        current = self._current_many(db, body["expected"])
        for path, expected in sorted(body["expected"].items()):
            actual = current[path]
            if actual != expected:
                differences[path] = {"expected": expected, "actual": actual}
        if differences:
            raise Conflict(differences)
        # Apply to CURRENT state, never replace it with the author's base tree.
        present = [(path, blob) for path, blob in body["writes"].items() if blob is not None]
        self._verify_blobs(db, (blob for _, blob in present))
        db.executemany("DELETE FROM current_files WHERE path=?", [(p,) for p in body["writes"]])
        if present:
            db.executemany("INSERT INTO current_files VALUES (?, ?)", present)
        # Validate the final namespace, allowing file -> directory changes.
        ancestors = set()
        for path, blob in present:
            parts = path.split("/")
            for count in range(1, len(parts)):
                ancestors.add("/".join(parts[:count]))
        for ancestor, blob in self._current_many(db, ancestors).items():
            if blob is not None:
                raise SeedError("File/directory collision at ancestor: " + ancestor)
        for start in range(0, len(present), 200):
            chunk = present[start:start + 200]
            values = ",".join("(?,?,?)" for _ in chunk)
            parameters = [part for path, _ in chunk for part in (path, path + "/", path + "0")]
            descendant = db.execute(
                f"WITH wanted(parent,first,last) AS (VALUES {values}) "
                "SELECT wanted.parent, files.path FROM wanted JOIN current_files files "
                "ON files.path>=wanted.first AND files.path<wanted.last LIMIT 1", parameters).fetchone()
            if descendant:
                raise SeedError(f"File/directory collision: {descendant[0]} and {descendant[1]}")
        revision = object_id("revision", canonical({"parent": parent, "proposal": proposal}))
        seq = self._revision(db, parent)["seq"] + 1
        db.execute("INSERT INTO revisions VALUES (?, ?, ?, ?)",
                   (seq, revision, parent, proposal))
        db.executemany("INSERT INTO versions VALUES (?, ?, ?)",
                       [(path, seq, blob) for path, blob in body["writes"].items()])
        db.execute("UPDATE meta SET value=? WHERE key='head'", (revision,))
        return {"revision": revision, "status": "landed"}

    def read(self, path: str, revision: str | None = None) -> bytes | None:
        file_path(path)
        with self._db() as db:
            if revision is None:
                blob = self._current(db, path)
            else:
                blob = self._at(db, path, self._revision(db, revision)["seq"])
            return None if blob is None else self._blob(db, blob)

    def manifest(self, revision: str | None = None) -> dict[str, str]:
        with self._db() as db:
            if revision is None:
                rows = db.execute("SELECT path, blob FROM current_files ORDER BY path")
            else:
                seq = self._revision(db, revision)["seq"]
                rows = db.execute("SELECT v.path, v.blob FROM versions v JOIN "
                                  "(SELECT path, MAX(seq) seq FROM versions WHERE seq<=? "
                                  "GROUP BY path) latest USING(path, seq) "
                                  "WHERE v.blob IS NOT NULL ORDER BY v.path", (seq,))
            return {row[0]: row[1] for row in rows}

    def describe(self, proposal: str) -> dict:
        with self._db() as db:
            return self._proposal(db, proposal)

    def snapshot(self, paths: Iterable[str], revision: str | None = None) -> dict:
        paths = {file_path(p) for p in paths}
        with self._db() as db:
            revision = revision or self._head(db)
            seq = self._revision(db, revision)["seq"]
            return {"revision": revision, "files": self._at_many(db, paths, seq)}

    def file_page(self, *, revision: str | None = None, after: str = "", limit: int = 100) -> dict:
        if not 1 <= limit <= 1000:
            raise SeedError("Page limit must be between 1 and 1000")
        with self._db() as db:
            revision = revision or self._head(db)
            seq = self._revision(db, revision)["seq"]
            found = {}
            cursor = after
            while len(found) <= limit:
                candidates = list(db.execute("SELECT DISTINCT path FROM versions WHERE path>? "
                                             "ORDER BY path LIMIT ?", (cursor, limit + 1)))
                if not candidates:
                    break
                paths = [row["path"] for row in candidates]
                for path, blob in self._at_many(db, paths, seq).items():
                    if blob is not None:
                        found[path] = blob
                cursor = paths[-1]
            items = list(found.items())[:limit]
            return {"revision": revision, "files": dict(items),
                    "next": items[-1][0] if len(found) > limit else None}

    def log_page(self, *, after: int = -1, limit: int = 100) -> dict:
        if after < -1 or not 1 <= limit <= 1000:
            raise SeedError("Invalid history page")
        with self._db() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM revisions WHERE seq>? ORDER BY seq LIMIT ?", (after, limit + 1))]
            return {"revisions": rows[:limit], "next": rows[limit-1]["seq"] if len(rows) > limit else None}

    def log(self) -> list[dict]:
        with self._db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM revisions ORDER BY seq")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="seed.db")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "head", "log"):
        commands.add_parser(name)
    propose = commands.add_parser("propose")
    propose.add_argument("--base", required=True, help="immutable revision ID")
    propose.add_argument("--set", action="append", default=[], metavar="PATH=LOCAL_FILE")
    propose.add_argument("--delete", action="append", default=[], metavar="PATH")
    propose.add_argument("--read", action="append", default=[], metavar="PATH")
    propose.add_argument("--author", default="")
    propose.add_argument("--message", default="")
    land = commands.add_parser("land")
    land.add_argument("proposal")
    show = commands.add_parser("show")
    show.add_argument("proposal")
    listing = commands.add_parser("ls")
    listing.add_argument("--rev")
    cat = commands.add_parser("cat")
    cat.add_argument("path")
    cat.add_argument("--rev")
    args = parser.parse_args(argv)
    try:
        store = Store.create(args.db) if args.command == "init" else Store(args.db)
        if args.command in ("init", "head"):
            result = store.head()
        elif args.command == "propose":
            edits = {}
            for item in args.set:
                path, separator, local = item.partition("=")
                if not separator or not local:
                    raise SeedError("--set requires PATH=LOCAL_FILE")
                if path in edits:
                    raise SeedError("Duplicate edit path: " + path)
                edits[path] = Path(local).read_bytes()
            for path in args.delete:
                if path in edits:
                    raise SeedError("Duplicate edit path: " + path)
                edits[path] = None
            result = store.propose(args.base, edits, reads=args.read,
                                   author=args.author, message=args.message)
        elif args.command == "land":
            result = store.land(args.proposal)
        elif args.command == "show":
            result = store.describe(args.proposal)
        elif args.command == "log":
            result = store.log()
        elif args.command == "ls":
            result = store.manifest(args.rev)
        else:
            payload = store.read(args.path, args.rev)
            if payload is None:
                raise SeedError("File absent: " + args.path)
            sys.stdout.buffer.write(payload)
            return 0
        print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Conflict as exc:
        print(json.dumps({"status": "conflict", "paths": exc.differences}), file=sys.stderr)
        return 2
    except (SeedError, OSError, sqlite3.DatabaseError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
