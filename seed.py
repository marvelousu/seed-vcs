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
            seq = self._revision(db, base)["seq"]
            writes = {}
            expected = {path: self._at(db, path, seq) for path in read_paths}
            for path, payload in sorted(edits.items()):
                file_path(path)
                if payload is not None and not isinstance(payload, bytes):
                    raise SeedError("File contents must be bytes or None")
                before = self._at(db, path, seq)
                after = None if payload is None else object_id("blob", payload)
                if before == after:
                    continue
                writes[path] = after
                expected[path] = before
                if payload is not None:
                    db.execute("INSERT OR IGNORE INTO blobs VALUES (?, ?)", (after, payload))
            if not writes:
                raise SeedError("No effective writes; conditions-only proposals are unsupported")
            body = canonical({"format": 1, "base": base, "expected": expected,
                              "writes": writes, "author": author, "message": message})
            identifier = object_id("proposal", body)
            db.execute("INSERT OR IGNORE INTO proposals VALUES (?, ?)", (identifier, body))
            return identifier

    def land(self, proposal: str) -> dict:
        """Check and publish the entire proposal, or change nothing."""
        with self._db(write=True) as db:
            body = self._proposal(db, proposal)
            landed = db.execute("SELECT id FROM revisions WHERE proposal=?", (proposal,)).fetchone()
            if landed:
                return {"revision": landed[0], "status": "already-landed"}
            parent = self._head(db)
            differences = {}
            for path, expected in sorted(body["expected"].items()):
                actual = self._current(db, path)
                if actual != expected:
                    differences[path] = {"expected": expected, "actual": actual}
            if differences:
                raise Conflict(differences)
            # Apply to CURRENT state, never replace it with the author's base tree.
            for path in body["writes"]:
                db.execute("DELETE FROM current_files WHERE path=?", (path,))
            for path, blob in body["writes"].items():
                if blob is not None:
                    self._blob(db, blob)
                    db.execute("INSERT INTO current_files VALUES (?, ?)", (path, blob))
            # Validate the final namespace, allowing file -> directory changes.
            for path, blob in body["writes"].items():
                if blob is None:
                    continue
                parts = path.split("/")
                for count in range(1, len(parts)):
                    ancestor = "/".join(parts[:count])
                    if self._current(db, ancestor) is not None:
                        raise SeedError(f"File/directory collision: {ancestor} and {path}")
                descendant = db.execute(
                    "SELECT path FROM current_files WHERE path>=? AND path<? LIMIT 1",
                    (path + "/", path + "0")).fetchone()
                if descendant:
                    raise SeedError(f"File/directory collision: {path} and {descendant[0]}")
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
