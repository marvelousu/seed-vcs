"""Safe, explicitly-scoped local workspaces for Seed agent tasks.

This module deliberately does not try to trace arbitrary process reads.  A
workspace is an opt-in copy of one logical subtree; running in it establishes
that subtree as a conservative dependency through ``Session.files``.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import stat
from typing import Any

from seed import SeedError, file_path, object_id


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _reparse_or_symlink(path: Path) -> bool:
    """Return true for links and Windows reparse points without following them."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _validate_component(component: str) -> None:
    if not component or component.endswith((" ", ".")):
        raise SeedError(f"Unsafe Windows workspace path component: {component!r}")
    if any(ord(character) < 32 or character in '<>:"\\|?*' for character in component):
        raise SeedError(f"Unsafe Windows workspace path component: {component!r}")
    stem = component.split(".", 1)[0].upper()
    if stem in _RESERVED:
        raise SeedError(f"Reserved Windows workspace path component: {component!r}")


def _validate_logical_path(path: str) -> None:
    file_path(path)
    for component in path.split("/"):
        _validate_component(component)


def _under_prefix(path: str, prefix: str) -> bool:
    return not prefix or path == prefix or path.startswith(prefix + "/")


def _relative_for_prefix(path: str, prefix: str) -> str:
    if not _under_prefix(path, prefix):
        raise SeedError(f"Path {path!r} is outside workspace prefix {prefix!r}")
    relative = path[len(prefix):].lstrip("/") if prefix else path
    if not relative:
        raise SeedError("A workspace prefix must name files beneath it, not a file")
    return relative


def _safe_destination(root: Path, relative: str) -> Path:
    for component in relative.split("/"):
        _validate_component(component)
    destination = root.joinpath(*relative.split("/"))
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise SeedError("Workspace path escapes its root") from exc
    return destination


def _validate_case_hierarchy(paths: list[str]) -> None:
    """Reject collisions in every component, not just complete path names.

    Windows may map ``A/x`` and ``a/y`` into the same physical directory.  A
    complete-path-only comparison misses that, and a file named ``A`` can also
    collide with the parent component of ``a/x``.
    """
    seen: dict[str, str] = {}
    for path in paths:
        components = path.split("/")
        for length in range(1, len(components) + 1):
            actual = "/".join(components[:length])
            folded = actual.casefold()
            previous = seen.setdefault(folded, actual)
            if previous != actual:
                raise SeedError("Case-insensitive path collision in workspace scope")


def _assert_safe_parents(root: Path) -> None:
    """Do not create a workspace through an existing symlink/reparse point."""
    current = root.parent
    while True:
        if _reparse_or_symlink(current):
            raise SeedError(f"Workspace parent is a symlink or reparse point: {current}")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _manifest_path(state_path: Path) -> Path:
    return state_path.with_name(state_path.name + ".workspace.json")


def workspace_manifest(state_path: str | Path) -> dict[str, Any] | None:
    """Return the task's workspace manifest after validating its small schema."""
    path = _manifest_path(Path(state_path))
    if not path.exists():
        return None
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SeedError("Invalid workspace manifest") from exc
    if result.get("format") != "seed-agent/workspace/1":
        raise SeedError("Unsupported workspace manifest format")
    return result


def materialize_workspace(session: Any, root: str | Path, *, prefix: str = "", state_path: str | Path) -> dict[str, Any]:
    """Create an empty workspace from the task's base plus its current overlay.

    ``root`` must not exist.  Refusing an existing directory makes the action
    non-destructive and prevents accidentally importing a caller's unrelated
    source tree.
    """
    root_path = Path(root).expanduser().absolute()
    if root_path.exists() or root_path.is_symlink():
        raise SeedError("Workspace path already exists; choose a new empty path")
    _assert_safe_parents(root_path)
    if prefix:
        # The public CLI accepts either ``src`` or ``src/`` for a scope.
        prefix = file_path(prefix.rstrip("/"))

    # A command can inspect more than the names returned by a listing, so use a
    # full subtree guard here.  ``files`` additionally records its normal
    # membership observation.
    # Session.files returns the view after local edits, which is exactly what
    # is materialized.
    base_snapshot = session.track_scope(prefix, "subtree")
    paths = list(session.files(prefix=prefix))
    normalized: dict[str, str] = {}
    for path in paths:
        _validate_logical_path(path)
        relative = _relative_for_prefix(path, prefix)
        key = relative.casefold()
        previous = normalized.setdefault(key, path)
        if previous != path:
            raise SeedError("Case-insensitive path collision in workspace scope")
    _validate_case_hierarchy([_relative_for_prefix(path, prefix) for path in paths])

    root_path.mkdir()
    base_files: dict[str, dict[str, str]] = {}
    try:
        for path in base_snapshot["paths"]:
            _validate_logical_path(path)
            blob = base_snapshot["files"].get(path)
            if not isinstance(blob, str):
                raise SeedError("Range snapshot referenced an invalid base blob")
            # A subtree fingerprint already guards every base file.  Keep the
            # immutable blob ID as metadata; the materialization loop fetches
            # each payload once, without adding per-file read predicates.
            base_files[path] = {"blob": blob}
        for path in sorted(paths):
            relative = _relative_for_prefix(path, prefix)
            destination = _safe_destination(root_path, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if path in session.edits:
                payload = session.edits[path]
            else:
                blob = base_files.get(path, {}).get("blob")
                if not isinstance(blob, str):
                    raise SeedError("Workspace view has a base file outside its subtree snapshot")
                payload = session.client.read_blob(blob)
            if payload is None:
                # A local deletion can hide a file returned by a base listing.
                continue
            destination.write_bytes(payload)
    except BaseException:
        # Do not clean up automatically: we only created a new, known path, but
        # leaving it visible is safer than recursive deletion during failures.
        raise

    manifest = {
        "format": "seed-agent/workspace/1",
        "root": str(root_path),
        "prefix": prefix,
        "base": session.base,
        # This is the pinned-base digest, not the materialized overlay digest.
        # Repeated runs therefore recompute all scope edits from base and can
        # correctly represent a user reverting an earlier local edit.
        "base_files": base_files,
    }
    manifest_path = _manifest_path(Path(state_path))
    if manifest_path.exists():
        raise SeedError("Workspace manifest already exists for this task")
    _atomic_json(manifest_path, manifest)
    return {"workspace": str(root_path), "prefix": prefix, "files": len(base_files), "manifest": str(manifest_path)}


def collect_workspace_edits(root: str | Path, *, state_path: str | Path) -> dict[str, bytes | None]:
    """Return modifications under the materialized scope and no other paths."""
    manifest_path = _manifest_path(Path(state_path))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SeedError("Missing or invalid workspace manifest") from exc
    if manifest.get("format") != "seed-agent/workspace/1":
        raise SeedError("Unsupported workspace manifest format")
    root_path = Path(root).expanduser().absolute()
    if str(root_path) != manifest.get("root"):
        raise SeedError("Workspace does not match this task's manifest")
    if not root_path.is_dir() or _reparse_or_symlink(root_path):
        raise SeedError("Workspace root is missing or unsafe")

    prefix = manifest["prefix"]
    observed: dict[str, bytes] = {}
    folded: dict[str, str] = {}
    for directory, child_dirs, child_files in os.walk(root_path, followlinks=False):
        current = Path(directory)
        if _reparse_or_symlink(current):
            raise SeedError(f"Symlink or reparse point in workspace: {current}")
        for name in [*child_dirs, *child_files]:
            child = current / name
            if _reparse_or_symlink(child):
                raise SeedError(f"Symlink or reparse point in workspace: {child}")
        for name in child_files:
            child = current / name
            details = child.lstat()
            if not stat.S_ISREG(details.st_mode):
                raise SeedError(f"Non-regular file in workspace: {child}")
            relative = child.relative_to(root_path).as_posix()
            for component in relative.split("/"):
                _validate_component(component)
            logical = f"{prefix}/{relative}" if prefix else relative
            _validate_logical_path(logical)
            key = relative.casefold()
            prior = folded.setdefault(key, logical)
            if prior != logical:
                raise SeedError("Case-insensitive path collision in workspace")
            observed[logical] = child.read_bytes()

    baseline = manifest.get("base_files")
    if not isinstance(baseline, dict):
        raise SeedError("Workspace manifest has no pinned base files")
    _validate_case_hierarchy([
        _relative_for_prefix(path, prefix) for path in observed
    ])
    edits: dict[str, bytes | None] = {}
    for path in sorted(set(baseline) | set(observed)):
        payload = observed.get(path)
        original = baseline.get(path)
        if original is None:
            edits[path] = payload
        elif payload is None:
            edits[path] = None
        elif not isinstance(original, dict) or not isinstance(original.get("blob"), str):
            raise SeedError("Workspace manifest has an invalid pinned base file")
        elif object_id("blob", payload) != original["blob"]:
            edits[path] = payload
    return edits


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with open(temporary, "xb") as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
