"""Logical backup, verification, and restore for one Seed repository.

An archive is a newly-created directory containing canonical ``manifest.json``
and content-addressed blob files. It is intentionally not a copy of a live
SQLite database or PostgreSQL schema: each backend is read through its Store
snapshot contract, then reconstructed into a fresh repository.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from typing import Any

from seed import SeedError, Store, canonical, file_path, object_id


ARCHIVE_FORMAT = "seed-backup/1"
MAX_ARCHIVE_BLOBS = 100_000
MAX_ARCHIVE_RECORDS = 1_000_000
MAX_BLOB_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
IDENTIFIER = re.compile(r"[0-9a-f]{64}\Z")
FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _identifier(value: object, *, label: str = "object ID") -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise SeedError(f"Invalid {label}")
    return value


def _is_reparse_point(info: os.stat_result) -> bool:
    """Detect symlinks and Windows junctions without following either."""
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT))


def _archive_file(root: Path, identifier: str) -> Path:
    identifier = _identifier(identifier, label="blob ID")
    return root / "blobs" / identifier[:2] / identifier[2:]


def _regular_file(path: Path, *, maximum: int) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SeedError("Archive file is missing: " + str(path)) from exc
    if not stat.S_ISREG(info.st_mode) or _is_reparse_point(info):
        raise SeedError("Archive path is not a regular file: " + str(path))
    if info.st_size > maximum:
        raise SeedError("Archive file exceeds its size limit: " + str(path))
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SeedError("Cannot read archive file: " + str(path)) from exc


def _archive_root(path: str | Path) -> Path:
    root = Path(path)
    try:
        info = root.lstat()
    except OSError as exc:
        raise SeedError("Archive does not exist: " + str(root)) from exc
    if not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info):
        raise SeedError("Archive must be a real directory")
    return root.resolve()


def _decode_payload(value: object) -> bytes:
    if not isinstance(value, str):
        raise SeedError("Proposal payload is not base64 text")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise SeedError("Invalid base64 proposal payload") from exc


def _proposal_writes(body: dict[str, Any]) -> dict[str, str | None]:
    values = body.get("writes")
    if not isinstance(values, dict):
        raise SeedError("Damaged proposal writes")
    writes = {}
    for path, value in values.items():
        path = file_path(path)
        if value is not None:
            value = _identifier(value, label="proposal write blob ID")
        writes[path] = value
    return writes


def _blob_references(body: dict[str, Any]) -> set[str]:
    """Return blob IDs in proposal conditions and writes, excluding revision IDs."""
    values = body.get("expected")
    if not isinstance(values, dict):
        raise SeedError("Damaged proposal expected")
    references = set(_proposal_writes(body).values())
    references.discard(None)
    for value in values.values():
        if isinstance(value, str):
            references.add(_identifier(value, label="proposal expected blob ID"))
        elif value is not None and value != {"requires": 2}:
            raise SeedError("Damaged proposal expected value")
    return references


def _snapshot(store: Store) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Read one metadata snapshot and validate all immutable referenced bytes."""
    with store._db() as db:
        meta = [[row["key"], row["value"]]
                for row in db.execute("SELECT key, value FROM meta ORDER BY key")]
        proposals = []
        blob_ids = set()
        for row in db.execute("SELECT id, payload FROM proposals ORDER BY id"):
            identifier = _identifier(row["id"], label="proposal ID")
            payload = bytes(row["payload"])
            if object_id("proposal", payload) != identifier:
                raise SeedError("Missing or damaged proposal: " + identifier)
            body = store._proposal(db, identifier)
            proposals.append({"id": identifier,
                              "payload": base64.b64encode(payload).decode("ascii")})
            blob_ids.update(_blob_references(body))
        revisions = [dict(row) for row in db.execute(
            "SELECT seq, id, parent, proposal FROM revisions ORDER BY seq")]
        current_files = [dict(row) for row in db.execute(
            "SELECT path, blob FROM current_files ORDER BY path")]
        versions = [dict(row) for row in db.execute(
            "SELECT path, seq, blob FROM versions ORDER BY path, seq")]
        for row in current_files:
            blob_ids.add(_identifier(row["blob"], label="current blob ID"))
        for row in versions:
            file_path(row["path"])
            if row["blob"] is not None:
                blob_ids.add(_identifier(row["blob"], label="version blob ID"))
        if len(proposals) + len(revisions) + len(current_files) + len(versions) > MAX_ARCHIVE_RECORDS:
            raise SeedError("Archive record limit exceeded")
        if len(blob_ids) > MAX_ARCHIVE_BLOBS:
            raise SeedError("Archive blob count limit exceeded")
        blobs = {}
        total = 0
        for identifier in sorted(blob_ids):
            payload = store._blob(db, identifier)
            if len(payload) > MAX_BLOB_BYTES:
                raise SeedError("Blob exceeds backup size limit: " + identifier)
            total += len(payload)
            if total > MAX_ARCHIVE_BYTES:
                raise SeedError("Archive byte limit exceeded")
            blobs[identifier] = payload
        manifest = {
            "format": ARCHIVE_FORMAT,
            "store_format": store.FORMAT,
            "meta": meta,
            "proposals": proposals,
            "revisions": revisions,
            "current_files": current_files,
            "versions": versions,
            "blobs": [{"id": identifier, "size": len(payload)}
                      for identifier, payload in sorted(blobs.items())],
        }
        return manifest, blobs


def _write_archive(root: Path, manifest: dict[str, Any], blobs: dict[str, bytes]) -> None:
    for identifier, payload in sorted(blobs.items()):
        destination = _archive_file(root, identifier)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    payload = canonical(manifest)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise SeedError("Archive manifest exceeds its size limit")
    destination = root / "manifest.json"
    with destination.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def backup(store: Store, path: str | Path) -> dict[str, Any]:
    """Create a new logical archive directory without altering an existing one."""
    destination = Path(path)
    if destination.exists() or destination.is_symlink():
        raise SeedError("Backup destination already exists")
    try:
        destination.mkdir(parents=False)
    except OSError as exc:
        raise SeedError("Cannot create backup destination") from exc
    try:
        manifest, blobs = _snapshot(store)
        _write_archive(destination, manifest, blobs)
        # Validate bytes from disk before reporting a usable archive.
        verified = verify_archive(destination)
        return {"archive": str(destination), "head": dict(verified["meta"])["head"],
                "revisions": len(verified["revisions"]),
                "proposals": len(verified["proposals"]), "blobs": len(verified["blobs"])}
    except BaseException:
        # A partial archive is deliberately left in place and cannot be mistaken
        # for a valid one because manifest.json is written last.
        raise


def _required_mapping(items: object, *, label: str) -> dict[str, str]:
    if not isinstance(items, list):
        raise SeedError(label + " must be a list")
    result = {}
    for item in items:
        if (not isinstance(item, list) or len(item) != 2
                or not isinstance(item[0], str) or not isinstance(item[1], str)
                or item[0] in result):
            raise SeedError("Invalid " + label)
        result[item[0]] = item[1]
    return result


def _validate_archive_tree(root: Path, identifiers: set[str]) -> None:
    expected = {Path("manifest.json"), Path("blobs")}
    for identifier in identifiers:
        expected.add(Path("blobs") / identifier[:2])
        expected.add(Path("blobs") / identifier[:2] / identifier[2:])
    directories = {Path("blobs")}
    directories.update(Path("blobs") / identifier[:2] for identifier in identifiers)

    def walk(directory: Path) -> None:
        try:
            with os.scandir(directory) as entries:
                children = list(entries)
        except OSError as exc:
            raise SeedError("Cannot inspect archive directory: " + str(directory)) from exc
        for entry in children:
            child = Path(entry.path)
            relative = child.relative_to(root)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise SeedError("Cannot inspect archive path: " + str(relative)) from exc
            if _is_reparse_point(info) or relative not in expected:
                raise SeedError("Unexpected or unsafe archive path: " + str(relative))
            if relative in directories:
                if not stat.S_ISDIR(info.st_mode):
                    raise SeedError("Archive directory is not real: " + str(relative))
                # Recurse only after rejecting a symlink or Windows junction.
                walk(child)
            elif not stat.S_ISREG(info.st_mode):
                raise SeedError("Archive file is not regular: " + str(relative))

    walk(root)


def _validate_manifest(manifest: object, root: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    if not isinstance(manifest, dict) or set(manifest) != {
        "format", "store_format", "meta", "proposals", "revisions",
        "current_files", "versions", "blobs",
    }:
        raise SeedError("Unsupported archive manifest")
    if manifest["format"] != ARCHIVE_FORMAT or manifest["store_format"] not in ("seed/1", "seed/pg1"):
        raise SeedError("Unsupported archive format")
    meta = _required_mapping(manifest["meta"], label="archive metadata")
    if set(("format", "head")) - set(meta):
        raise SeedError("Archive metadata lacks format or head")
    allowed_formats = {
        "seed/1": {"seed/1", "seed/2"},
        "seed/pg1": {"seed/pg1", "seed/pg2"},
    }
    if meta["format"] not in allowed_formats[manifest["store_format"]]:
        raise SeedError("Archive repository format does not match backend")

    proposals = manifest["proposals"]
    revisions = manifest["revisions"]
    current_files = manifest["current_files"]
    versions = manifest["versions"]
    blobs = manifest["blobs"]
    if not all(isinstance(rows, list) for rows in (proposals, revisions, current_files, versions, blobs)):
        raise SeedError("Archive records must be lists")
    if sum(map(len, (proposals, revisions, current_files, versions))) > MAX_ARCHIVE_RECORDS:
        raise SeedError("Archive record limit exceeded")
    if len(blobs) > MAX_ARCHIVE_BLOBS:
        raise SeedError("Archive blob count limit exceeded")

    proposal_ids = set()
    proposal_bodies = {}
    proposal_bases = {}
    referenced = set()
    for row in proposals:
        if not isinstance(row, dict) or set(row) != {"id", "payload"}:
            raise SeedError("Invalid proposal record")
        identifier = _identifier(row["id"], label="proposal ID")
        if identifier in proposal_ids:
            raise SeedError("Duplicate proposal record")
        payload = _decode_payload(row["payload"])
        if object_id("proposal", payload) != identifier:
            raise SeedError("Proposal hash does not match archive")
        try:
            body = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SeedError("Invalid proposal payload") from exc
        if not isinstance(body, dict) or body.get("format") not in (1, 2):
            raise SeedError("Unsupported proposal in archive")
        proposal_ids.add(identifier)
        proposal_bodies[identifier] = body
        proposal_bases[identifier] = _identifier(
            body.get("base"), label="proposal base revision ID")
        referenced.update(_blob_references(body))
    revision_ids = set()
    previous = None
    for sequence, row in enumerate(revisions):
        if (not isinstance(row, dict) or set(row) != {"seq", "id", "parent", "proposal"}
                or row["seq"] != sequence):
            raise SeedError("Invalid revision record")
        identifier = _identifier(row["id"], label="revision ID")
        parent = row["parent"]
        proposal = row["proposal"]
        if parent is not None:
            parent = _identifier(parent, label="parent revision ID")
        if proposal is not None:
            proposal = _identifier(proposal, label="revision proposal ID")
        if object_id("revision", canonical({"parent": parent, "proposal": proposal})) != identifier:
            raise SeedError("Revision hash does not match archive")
        if sequence == 0:
            if parent is not None or proposal is not None:
                raise SeedError("Archive has no valid root revision")
        elif parent != previous or proposal not in proposal_ids:
            raise SeedError("Archive revision chain is invalid")
        if identifier in revision_ids:
            raise SeedError("Duplicate revision record")
        revision_ids.add(identifier)
        previous = identifier
    if not revisions or meta["head"] != previous:
        raise SeedError("Archive head does not match history")
    for identifier, base in proposal_bases.items():
        if base not in revision_ids:
            raise SeedError("Proposal base does not exist: " + identifier)

    version_values = {}
    for row in versions:
        if not isinstance(row, dict) or set(row) != {"path", "seq", "blob"}:
            raise SeedError("Invalid version record")
        path = file_path(row["path"])
        if not isinstance(row["seq"], int) or row["seq"] < 1 or row["seq"] >= len(revisions):
            raise SeedError("Invalid version sequence")
        blob = row["blob"]
        if blob is not None:
            blob = _identifier(blob, label="version blob ID")
            referenced.add(blob)
        key = (path, row["seq"])
        if key in version_values:
            raise SeedError("Duplicate version record")
        version_values[key] = blob
    for sequence, revision in enumerate(revisions[1:], start=1):
        proposal = revision["proposal"]
        written = {path: blob for (path, seq), blob in version_values.items()
                   if seq == sequence}
        if written != _proposal_writes(proposal_bodies[proposal]):
            raise SeedError("Versions do not match revision proposal writes")
    effective = {}
    for (path, _), blob in sorted(version_values.items(), key=lambda item: (item[0][1], item[0][0])):
        if blob is None:
            effective.pop(path, None)
        else:
            effective[path] = blob
    restored_current = {}
    for row in current_files:
        if not isinstance(row, dict) or set(row) != {"path", "blob"}:
            raise SeedError("Invalid current file record")
        path = file_path(row["path"])
        blob = _identifier(row["blob"], label="current blob ID")
        if path in restored_current:
            raise SeedError("Duplicate current file")
        restored_current[path] = blob
        referenced.add(blob)
    if restored_current != effective:
        raise SeedError("Current files do not match history")

    blob_rows = []
    blob_ids = set()
    for row in blobs:
        if not isinstance(row, dict) or set(row) != {"id", "size"}:
            raise SeedError("Invalid blob record")
        identifier = _identifier(row["id"], label="blob ID")
        if identifier in blob_ids or not isinstance(row["size"], int) or row["size"] < 0:
            raise SeedError("Invalid or duplicate blob record")
        blob_ids.add(identifier)
        blob_rows.append((identifier, row["size"]))
    if referenced != blob_ids:
        raise SeedError("Archive blob set does not match metadata references")
    # Inspect every path before following any blob path.  In particular this
    # rejects Windows junctions, which pathlib's symlink predicate does not.
    _validate_archive_tree(root, blob_ids)
    blob_payloads = {}
    total = 0
    for identifier, size in blob_rows:
        payload = _regular_file(_archive_file(root, identifier), maximum=MAX_BLOB_BYTES)
        if len(payload) != size or object_id("blob", payload) != identifier:
            raise SeedError("Blob file does not match archive")
        total += len(payload)
        if total > MAX_ARCHIVE_BYTES:
            raise SeedError("Archive byte limit exceeded")
        blob_payloads[identifier] = payload
    return manifest, blob_payloads


def verify_archive(path: str | Path) -> dict[str, Any]:
    """Validate archive layout, canonical manifest, metadata, and all blob bytes."""
    root = _archive_root(path)
    payload = _regular_file(root / "manifest.json", maximum=MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SeedError("Invalid archive manifest JSON") from exc
    if canonical(manifest) != payload:
        raise SeedError("Archive manifest is not canonical JSON")
    manifest, _ = _validate_manifest(manifest, root)
    return manifest


def _fresh_target(store: Store, db) -> None:
    expected_root = object_id("revision", canonical({"parent": None, "proposal": None}))
    meta = {row["key"]: row["value"] for row in db.execute("SELECT key, value FROM meta")}
    root = db.execute("SELECT seq, id, parent, proposal FROM revisions").fetchone()
    counts = [db.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
              for table in ("blobs", "proposals", "current_files", "versions")]
    if (meta != {"format": store.FORMAT, "head": expected_root}
            or root is None or dict(root) != {"seq": 0, "id": expected_root,
                                               "parent": None, "proposal": None}
            or any(counts)):
        raise SeedError("Restore target is not a freshly created repository")


def restore(store: Store, path: str | Path) -> dict[str, Any]:
    """Restore a verified, trusted operational archive into a fresh Store.

    Verification proves archive integrity and self-consistency, not its origin
    or the provenance of preserved workflow attestations and pointers.
    """
    root = _archive_root(path)
    payload = _regular_file(root / "manifest.json", maximum=MAX_MANIFEST_BYTES)
    try:
        decoded = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SeedError("Invalid archive manifest JSON") from exc
    if canonical(decoded) != payload:
        raise SeedError("Archive manifest is not canonical JSON")
    manifest, blobs = _validate_manifest(decoded, root)
    if manifest["store_format"] != store.FORMAT:
        raise SeedError("Archive backend does not match restore target")
    metadata = _required_mapping(manifest["meta"], label="archive metadata")
    with store._db(write=True) as db:
        _fresh_target(store, db)
        for identifier, blob in sorted(blobs.items()):
            if store._put_blob(db, blob) != identifier:
                raise SeedError("Restore blob ID mismatch")
        db.executemany("INSERT INTO proposals VALUES (?, ?)", [
            (row["id"], _decode_payload(row["payload"])) for row in manifest["proposals"]
        ])
        db.executemany("INSERT INTO revisions VALUES (?, ?, ?, ?)", [
            (row["seq"], row["id"], row["parent"], row["proposal"])
            for row in manifest["revisions"][1:]
        ])
        db.executemany("INSERT INTO versions VALUES (?, ?, ?)", [
            (row["path"], row["seq"], row["blob"]) for row in manifest["versions"]
        ])
        db.executemany("INSERT INTO current_files VALUES (?, ?)", [
            (row["path"], row["blob"]) for row in manifest["current_files"]
        ])
        for key, value in metadata.items():
            if key in ("format", "head"):
                db.execute("UPDATE meta SET value=? WHERE key=?", (value, key))
            else:
                db.execute("INSERT INTO meta VALUES (?, ?)", (key, value))
    return {"head": metadata["head"], "revisions": len(manifest["revisions"]),
            "proposals": len(manifest["proposals"]), "blobs": len(manifest["blobs"])}


def _open_postgres(arguments, *, restore_target: bool):
    dsn = os.environ.get("SEED_DATABASE_URL")
    if not dsn:
        raise SeedError("SEED_DATABASE_URL is required for PostgreSQL backup commands")
    if not arguments.repository or not arguments.objects:
        raise SeedError("--repository and --objects are required for PostgreSQL")
    from seed_postgres import PostgresStore, make_pool

    pool = make_pool(dsn)
    try:
        if restore_target:
            store = PostgresStore.create(pool, arguments.repository, arguments.objects)
        else:
            store = PostgresStore(pool, arguments.repository, arguments.objects)
            with pool.connection() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name=%s", (store.schema,)
                ).fetchone()
            if not exists:
                raise SeedError("PostgreSQL repository does not exist")
            with store._db() as db:
                row = db.execute("SELECT value FROM meta WHERE key='format'").fetchone()
                if row is None or row["value"] not in (store.FORMAT, store.FORMAT_V2):
                    raise SeedError("Unsupported PostgreSQL repository format")
        return store, pool
    except BaseException:
        pool.close()
        raise


def _open_cli_store(arguments, *, restore_target: bool):
    if arguments.db:
        if arguments.repository or arguments.objects:
            raise SeedError("Use either --db or PostgreSQL --repository/--objects")
        database = Path(arguments.db)
        if restore_target and not database.exists():
            return Store.create(database), None
        return Store(database), None
    return _open_postgres(arguments, restore_target=restore_target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="SQLite database path")
    parser.add_argument("--repository", help="PostgreSQL repository name")
    parser.add_argument("--objects", help="PostgreSQL immutable object directory")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "verify", "restore"):
        command = commands.add_parser(name)
        command.add_argument("archive", help="archive directory")
    arguments = parser.parse_args(argv)
    pool = None
    try:
        if arguments.command == "verify":
            if arguments.db or arguments.repository or arguments.objects:
                raise SeedError("verify reads only the archive and takes no store options")
            result = verify_archive(arguments.archive)
            output = {"status": "verified", "head": dict(result["meta"])["head"],
                      "revisions": len(result["revisions"]), "blobs": len(result["blobs"])}
        else:
            store, pool = _open_cli_store(arguments, restore_target=arguments.command == "restore")
            result = backup(store, arguments.archive) if arguments.command == "backup" else restore(store, arguments.archive)
            output = {"status": arguments.command + "ed", **result}
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    except (SeedError, OSError, ValueError, sqlite3.DatabaseError) as exc:  # type: ignore[name-defined]
        print(json.dumps({"status": "error", "message": str(exc)}), file=sys.stderr)
        return 1
    finally:
        if pool is not None:
            pool.close()


if __name__ == "__main__":
    raise SystemExit(main())
