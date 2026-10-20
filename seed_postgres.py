"""PostgreSQL metadata and immutable file objects for the Seed store contract."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import tempfile

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from seed import SeedError, Store, canonical, object_id


class Row(dict):
    def __getitem__(self, key):
        return (
            list(self.values())[key]
            if isinstance(key, int)
            else super().__getitem__(key)
        )


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def fetchone(self):
        row = self.cursor.fetchone()
        return None if row is None else Row(row)

    def __iter__(self):
        return (Row(row) for row in self.cursor)


class Database:
    """Adapt the small, fixed SQL vocabulary shared with the SQLite kernel."""

    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def sql(query):
        query = query.replace("?", "%s")
        if query.startswith("INSERT OR IGNORE INTO "):
            query = query.replace("INSERT OR IGNORE INTO ", "INSERT INTO ", 1)
            query += " ON CONFLICT DO NOTHING"
        return query

    def execute(self, query, parameters=()):
        return Cursor(self.connection.execute(self.sql(query), parameters))

    def executemany(self, query, parameters):
        with self.connection.cursor() as cursor:
            cursor.executemany(self.sql(query), parameters)


def make_pool(dsn: str, max_size: int = 16):
    return ConnectionPool(
        dsn,
        min_size=1,
        max_size=max_size,
        timeout=10,
        max_waiting=max_size * 4,
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=True,
    )


class ObjectDirectory:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[0-9a-f]{64}", identifier
        ):
            raise SeedError("Invalid blob ID")
        return self.root / identifier[:2] / identifier[2:]

    def put(self, payload):
        identifier = object_id("blob", payload)
        destination = self.path(identifier)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            self.read(identifier)
            return identifier
        # Atomically publish complete bytes before registering them in PostgreSQL.
        descriptor, temporary = tempfile.mkstemp(
            dir=destination.parent, prefix="upload-"
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            if os.name != "nt":
                directory = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return identifier

    def read(self, identifier):
        try:
            payload = self.path(identifier).read_bytes()
        except OSError as exc:
            raise SeedError("Missing blob object: " + identifier) from exc
        if object_id("blob", payload) != identifier:
            raise SeedError("Damaged blob object: " + identifier)
        return payload


PG_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE blobs (id TEXT PRIMARY KEY, size BIGINT NOT NULL CHECK(size>=0));
CREATE TABLE proposals (id TEXT PRIMARY KEY, payload BYTEA NOT NULL);
CREATE TABLE revisions (
    seq BIGINT PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    parent TEXT REFERENCES revisions(id),
    proposal TEXT UNIQUE REFERENCES proposals(id)
);
CREATE TABLE current_files (
    path TEXT COLLATE "C" PRIMARY KEY,
    blob TEXT NOT NULL REFERENCES blobs(id)
);
CREATE TABLE versions (
    path TEXT COLLATE "C" NOT NULL,
    seq BIGINT NOT NULL REFERENCES revisions(seq),
    blob TEXT REFERENCES blobs(id),
    PRIMARY KEY(path,seq)
);
"""


class PostgresStore(Store):
    FORMAT = "seed/pg1"
    FORMAT_V2 = "seed/pg2"

    def __init__(self, pool, repository, objects):
        self.pool = pool
        self.schema = "seed_" + hashlib.sha256(repository.encode()).hexdigest()[:32]
        self.objects = ObjectDirectory(Path(objects) / self.schema)
        # Schema names are generated from hashes, never interpolated user names.

    @classmethod
    def create(cls, pool, repository, objects):
        store = cls(pool, repository, objects)
        lock = int(store.schema[5:20], 16)
        with pool.connection() as connection, connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
            exists = connection.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name=%s",
                (store.schema,),
            ).fetchone()
            if not exists:
                connection.execute(f'CREATE SCHEMA "{store.schema}"')
                connection.execute(
                    f'SET LOCAL search_path TO "{store.schema}", pg_catalog'
                )
                for statement in PG_SCHEMA.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                root = object_id(
                    "revision", canonical({"parent": None, "proposal": None})
                )
                connection.execute(
                    "INSERT INTO revisions VALUES (0,%s,NULL,NULL)", (root,)
                )
                connection.execute(
                    "INSERT INTO meta VALUES ('format','seed/pg1'),('head',%s)", (root,)
                )
            else:
                connection.execute(
                    f'SET LOCAL search_path TO "{store.schema}", pg_catalog'
                )
                row = connection.execute(
                    "SELECT value FROM meta WHERE key='format'"
                ).fetchone()
                if row is None or row["value"] not in ("seed/pg1", "seed/pg2"):
                    raise SeedError("Unsupported PostgreSQL repository format")
        return store

    @contextmanager
    def _db(self, write=False):
        with self.pool.connection() as connection, connection.transaction():
            isolation = "READ COMMITTED" if write else "REPEATABLE READ READ ONLY"
            connection.execute("SET TRANSACTION ISOLATION LEVEL " + isolation)
            connection.execute(f'SET LOCAL search_path TO "{self.schema}", pg_catalog')
            connection.execute("SET LOCAL statement_timeout = '15s'")
            yield Database(connection)

    def _lock_head(self, db):
        row = db.execute(
            "SELECT value FROM meta WHERE key='head' FOR UPDATE"
        ).fetchone()
        if row is None:
            raise SeedError("Repository has no head")

    def _put_blob(self, db, payload):
        identifier = self.objects.put(payload)
        db.execute(
            "INSERT OR IGNORE INTO blobs VALUES (?, ?)", (identifier, len(payload))
        )
        return identifier

    def _verify_blob(self, db, identifier):
        self._verify_blobs(db, [identifier])

    def _verify_blobs(self, db, identifiers):
        identifiers = sorted(set(identifiers))
        for start in range(0, len(identifiers), 400):
            chunk = identifiers[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = {
                row["id"]: row["size"]
                for row in db.execute(
                    f"SELECT id,size FROM blobs WHERE id IN ({placeholders})", chunk
                )
            }
            for identifier in chunk:
                if identifier not in rows:
                    raise SeedError("Unknown blob: " + identifier)
                try:
                    size = self.objects.path(identifier).stat().st_size
                except OSError as exc:
                    raise SeedError("Missing blob: " + identifier) from exc
                if size != rows[identifier]:
                    raise SeedError("Damaged blob: " + identifier)

    def _blob(self, db, identifier):
        self._verify_blob(db, identifier)
        return self.objects.read(identifier)
