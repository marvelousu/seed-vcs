"""One-way, regular-file Git import/export bridge for Seed.

This is deliberately not a bidirectional synchronizer.  A Git commit may be
imported once into an empty Seed repository.  Seed revisions may be exported to
a dedicated ``refs/heads/seed/...`` branch, without checking out or modifying
any user branch or worktree.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable, Mapping

from seed import SeedError, Store, file_path
from seed_client import Client


_GIT_HEX = re.compile(r"[0-9a-fA-F]{4,64}\Z")
_SEED_ID = re.compile(r"[0-9a-f]{64}\Z")
_IMPORT_TRAILER = "Seed-Git-Import: "
_EXPORT_TRAILER = "Seed-Revision: "
MAX_IMPORT_FILES = 1_000
MAX_IMPORT_BLOB_BYTES = 1 * 1024 * 1024
MAX_IMPORT_TOTAL_BYTES = 8 * 1024 * 1024
_GIT_SAFE_OPTIONS = (
    "-c", f"core.hooksPath={os.devnull}",
    "-c", "commit.gpgsign=false",
    "-c", "tag.gpgSign=false",
)
_GIT_IDENTITY_VARIABLES = frozenset({
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_DATE",
})


def _git_environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an environment that cannot redirect Git to another repository.

    ``git -C`` alone is insufficient when a caller has set GIT_DIR, an index
    override, or a config override. The bridge owns neither those settings nor
    user hooks/signing policy, so plumbing commands run with all inherited
    GIT_* variables removed and a null global/system configuration.
    """
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    })
    if overrides:
        unexpected = set(overrides).difference(_GIT_IDENTITY_VARIABLES)
        if unexpected:
            raise SeedError("Git bridge accepts only explicit commit identity overrides")
        env.update(overrides)
    return env


def _git(root: str | Path, arguments: list[str], *, input_data: bytes | None = None,
         environment: Mapping[str, str] | None = None, optional: bool = False) -> bytes | None:
    """Run Git plumbing without a shell or inherited path-like user input."""
    root = Path(root).expanduser().resolve()
    command = ["git", "-C", str(root), *_GIT_SAFE_OPTIONS, *arguments]
    env = _git_environment(environment)
    options: dict[str, Any] = {}
    if input_data is not None:
        options["input"] = input_data
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=30,
            check=False,
            env=env,
            **options,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SeedError("Git command could not be completed") from exc
    if result.returncode:
        if optional:
            return None
        raise SeedError("Git command was rejected: " + arguments[0])
    return result.stdout


def _git_text(root: str | Path, arguments: list[str], **kwargs: Any) -> str | None:
    result = _git(root, arguments, **kwargs)
    if result is None:
        return None
    try:
        return result.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise SeedError("Git returned invalid text") from exc


def _resolve_commit(git_root: str | Path, commit: str) -> str:
    if not isinstance(commit, str) or not _GIT_HEX.fullmatch(commit):
        raise SeedError("Git import accepts only a hexadecimal commit ID")
    resolved = _git_text(
        git_root,
        ["rev-parse", "--verify", "--end-of-options", commit + "^{commit}"],
    )
    if resolved is None or not _GIT_HEX.fullmatch(resolved):
        raise SeedError("Git did not resolve a commit ID")
    return resolved.lower()


def _git_commit_files(git_root: str | Path, commit: str) -> dict[str, bytes]:
    """Read a recursive Git tree without treating file names as command text."""
    raw = _git(git_root, ["ls-tree", "-rz", "--full-tree", commit])
    assert raw is not None
    entries: list[tuple[str, str]] = []
    paths: set[str] = set()
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, identifier = metadata.split(b" ", 2)
        except ValueError as exc:
            raise SeedError("Git returned a malformed tree entry") from exc
        try:
            identifier_text = identifier.decode("ascii")
        except UnicodeDecodeError as exc:
            raise SeedError("Git returned an invalid object ID") from exc
        if mode != b"100644" or kind != b"blob" or not _GIT_HEX.fullmatch(identifier_text):
            raise SeedError(
                "Git import supports regular 100644 files only; "
                "symlinks, submodules, and executable files are unsupported"
            )
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SeedError("Git import requires UTF-8 file names") from exc
        file_path(path)
        if path in paths:
            raise SeedError("Git tree contains duplicate paths")
        paths.add(path)
        entries.append((path, identifier_text))
        if len(entries) > MAX_IMPORT_FILES:
            raise SeedError(f"Git import is limited to {MAX_IMPORT_FILES} files per Seed proposal")
    if not entries:
        raise SeedError("An empty Git commit cannot be represented by a Seed proposal")
    total = 0
    for _, identifier in entries:
        size_text = _git_text(git_root, ["cat-file", "-s", identifier])
        if size_text is None or not size_text.isascii() or not size_text.isdecimal():
            raise SeedError("Git returned an invalid blob size")
        size = int(size_text)
        if size > MAX_IMPORT_BLOB_BYTES:
            raise SeedError(f"Git import blob exceeds {MAX_IMPORT_BLOB_BYTES} bytes")
        total += size
        if total > MAX_IMPORT_TOTAL_BYTES:
            raise SeedError(f"Git import exceeds {MAX_IMPORT_TOTAL_BYTES} bytes")
    files: dict[str, bytes] = {}
    for path, identifier in entries:
        payload = _git(git_root, ["cat-file", "blob", identifier])
        assert payload is not None
        # The content-addressed object cannot legitimately change after its
        # size query; this also keeps a malformed Git response bounded.
        if len(payload) > MAX_IMPORT_BLOB_BYTES:
            raise SeedError(f"Git import blob exceeds {MAX_IMPORT_BLOB_BYTES} bytes")
        files[path] = payload
    return dict(sorted(files.items()))


def _seed_history(seed: Any) -> list[dict[str, Any]]:
    if hasattr(seed, "log"):
        return list(seed.log())
    rows: list[dict[str, Any]] = []
    after = -1
    while True:
        page = seed.history(after=after, limit=1000)
        page_rows = page.get("revisions")
        if not isinstance(page_rows, list):
            raise SeedError("Seed service returned invalid history")
        rows.extend(page_rows)
        cursor = page.get("next")
        if cursor is None:
            return rows
        if not isinstance(cursor, int) or cursor <= after:
            raise SeedError("Seed service returned an invalid history cursor")
        after = cursor


def _seed_imported_revision(seed: Any, commit: str) -> str | None:
    marker = _IMPORT_TRAILER + commit
    for row in reversed(_seed_history(seed)):
        proposal = row.get("proposal")
        if proposal is None:
            continue
        body = seed.describe(proposal)
        if marker in body.get("message", "").splitlines():
            return row["id"]
    return None


def _seed_empty(seed: Any) -> bool:
    history = _seed_history(seed)
    if len(history) != 1 or history[0].get("parent") is not None:
        return False
    if hasattr(seed, "manifest"):
        return not seed.manifest()
    page = seed.files(limit=1)
    return not page.get("files") and page.get("next") is None


def import_git_commit(seed: Any, git_root: str | Path, commit: str) -> dict[str, str]:
    """Import one verified Git commit into an empty Seed repository.

    The source commit trailer makes a retry after a successful landing return
    the original Seed revision.  Before landing, proposal identity is stable
    because the base, bytes, author, and message are all fixed.
    """
    commit = _resolve_commit(git_root, commit)
    previous = _seed_imported_revision(seed, commit)
    if previous is not None:
        return {"status": "already-imported", "commit": commit, "revision": previous}
    if not _seed_empty(seed):
        raise SeedError("Git import requires an empty Seed repository")
    files = _git_commit_files(git_root, commit)
    base = seed.head()
    proposal = seed.propose(
        base,
        files,
        author="seed-git",
        message="Import Git commit\n\n" + _IMPORT_TRAILER + commit,
    )
    landed = seed.land(proposal)
    return {
        "status": landed["status"],
        "commit": commit,
        "proposal": proposal,
        "revision": landed["revision"],
    }


def _seed_manifest(seed: Any, revision: str) -> dict[str, bytes]:
    if hasattr(seed, "manifest"):
        paths = seed.manifest(revision)
        return {path: seed.read(path, revision) for path in paths}
    files: dict[str, Any] = {}
    after = ""
    while True:
        page = seed.files(revision=revision, after=after, limit=1000)
        if page.get("revision") != revision:
            raise SeedError("Seed service returned files from another revision")
        page_files = page.get("files")
        if not isinstance(page_files, dict):
            raise SeedError("Seed service returned an invalid file page")
        if set(files).intersection(page_files):
            raise SeedError("Seed service repeated a file page")
        files.update(page_files)
        cursor = page.get("next")
        if cursor is None:
            break
        if not isinstance(cursor, str) or cursor <= after:
            raise SeedError("Seed service returned an invalid file cursor")
        after = cursor
    result: dict[str, bytes] = {}
    for path in sorted(files):
        payload = seed.read(path, revision)
        if payload is None:
            raise SeedError("Seed service mixed file contents across revisions")
        result[path] = payload
    return result


def _seed_revision(seed: Any, revision: str) -> dict[str, Any]:
    for row in _seed_history(seed):
        if row.get("id") == revision:
            return row
    raise SeedError("Seed revision was not found")


def _validate_branch(git_root: str | Path, branch: str) -> str:
    if not isinstance(branch, str) or not branch.startswith("seed/"):
        raise SeedError("Git export branch must start with 'seed/'")
    if any(token in branch for token in ("..", "@{", "//")) or branch.endswith("."):
        raise SeedError("Invalid dedicated Git export branch")
    _git(git_root, ["check-ref-format", "--branch", branch])
    return "refs/heads/" + branch


def _read_ref(git_root: str | Path, reference: str) -> str | None:
    value = _git_text(git_root, ["rev-parse", "--verify", "--quiet", reference], optional=True)
    if value is None:
        return None
    if not _GIT_HEX.fullmatch(value):
        raise SeedError("Git returned an invalid object ID")
    return value.lower()


def _symbolic_ref(git_root: str | Path, reference: str) -> str | None:
    """Return a symbolic ref target, rejecting an invalid target as absent."""
    value = _git_text(git_root, ["symbolic-ref", "-q", reference], optional=True)
    if value is None:
        return None
    if not value.startswith("refs/"):
        raise SeedError("Git returned an invalid symbolic ref")
    return value


def _require_direct_ref(git_root: str | Path, reference: str) -> None:
    if _symbolic_ref(git_root, reference) is not None:
        raise SeedError("Seed bridge refuses symbolic export refs")


def _commit_message(git_root: str | Path, commit: str) -> str:
    message = _git_text(git_root, ["show", "-s", "--format=%B", commit])
    if message is None:
        raise SeedError("Git commit was not found")
    return message


def _exported_revision(git_root: str | Path, commit: str) -> str | None:
    for line in _commit_message(git_root, commit).splitlines():
        if line.startswith(_EXPORT_TRAILER):
            revision = line.removeprefix(_EXPORT_TRAILER)
            if _SEED_ID.fullmatch(revision):
                return revision
    return None


def _commit_tree(git_root: str | Path, commit: str) -> str:
    tree = _git_text(git_root, ["rev-parse", "--verify", "--end-of-options", commit + "^{tree}"])
    if tree is None or not _GIT_HEX.fullmatch(tree):
        raise SeedError("Git export commit has an invalid tree")
    return tree.lower()


def _is_ancestor(git_root: str | Path, ancestor: str, descendant: str) -> bool:
    return _git(
        git_root,
        ["merge-base", "--is-ancestor", ancestor, descendant],
        optional=True,
    ) is not None


def _find_export(git_root: str | Path, branch_ref: str, revision: str) -> str | None:
    mapping_ref = "refs/seed/exports/" + revision
    _require_direct_ref(git_root, branch_ref)
    _require_direct_ref(git_root, mapping_ref)
    mapping = _read_ref(git_root, mapping_ref)
    tip = _read_ref(git_root, branch_ref)
    if tip is not None and _exported_revision(git_root, tip) is None:
        raise SeedError("Dedicated export branch is not owned by Seed")
    if mapping is not None:
        if tip is not None and not _is_ancestor(git_root, mapping, tip):
            raise SeedError("Seed export mapping is not on the dedicated branch")
        return mapping
    if tip is None:
        return None
    commits = _git_text(git_root, ["rev-list", branch_ref])
    assert commits is not None
    for commit in commits.splitlines():
        if _exported_revision(git_root, commit) == revision:
            return commit
    return None


def _object_format(git_root: str | Path) -> str:
    value = _git_text(git_root, ["rev-parse", "--show-object-format"])
    if value not in {"sha1", "sha256"}:
        raise SeedError("Unsupported Git object format")
    return value


def _hash_blob(git_root: str | Path, payload: bytes) -> str:
    result = _git_text(git_root, ["hash-object", "-w", "--stdin"], input_data=payload)
    if result is None or not _GIT_HEX.fullmatch(result):
        raise SeedError("Git did not create a blob")
    return result.lower()


def _write_tree(git_root: str | Path, files: Mapping[str, bytes]) -> str:
    root: dict[str, Any] = {}
    for path, payload in files.items():
        file_path(path)
        node = root
        parts = path.split("/")
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise SeedError("Seed manifest has a file/directory collision")
            node = child
        if parts[-1] in node:
            raise SeedError("Seed manifest has duplicate paths")
        node[parts[-1]] = payload

    def emit(node: dict[str, Any]) -> str:
        entries: list[tuple[bytes, bytes]] = []
        for name, value in node.items():
            encoded_name = name.encode("utf-8")
            if b"\0" in encoded_name or b"/" in encoded_name:
                raise SeedError("Seed path cannot be written to a Git tree")
            if isinstance(value, dict):
                identifier, mode, kind = emit(value), b"040000", b"tree"
                order_name = encoded_name + b"/"
            else:
                if not isinstance(value, bytes):
                    raise SeedError("Seed manifest contains invalid file bytes")
                identifier, mode, kind = _hash_blob(git_root, value), b"100644", b"blob"
                order_name = encoded_name
            entry = mode + b" " + kind + b" " + identifier.encode("ascii") + b"\t" + encoded_name + b"\0"
            entries.append((order_name, entry))
        raw = b"".join(entry for _, entry in sorted(entries))
        result = _git_text(git_root, ["mktree", "-z"], input_data=raw)
        if result is None or not _GIT_HEX.fullmatch(result):
            raise SeedError("Git did not create a tree")
        return result.lower()

    return emit(root)


def _update_ref(git_root: str | Path, reference: str, new: str, old: str | None) -> None:
    _require_direct_ref(git_root, reference)
    zero = "0" * (64 if _object_format(git_root) == "sha256" else 40)
    # `--no-deref` makes a replacement of the ref itself the worst possible
    # race outcome if somebody turns it into a symref after the check above;
    # it must never advance the symref target (for example, main).
    _git(git_root, ["update-ref", "--no-deref", reference, new, old or zero])


def _bridge_commit(git_root: str | Path, tree: str, parent: str | None, revision: str) -> str:
    arguments = ["commit-tree", tree, "-F", "-"]
    if parent is not None:
        arguments.extend(["-p", parent])
    message = (
        "Export Seed revision\n\n"
        + _EXPORT_TRAILER + revision + "\n"
    ).encode("utf-8")
    identity = {
        "GIT_AUTHOR_NAME": "Seed Bridge",
        "GIT_AUTHOR_EMAIL": "seed-git@localhost",
        "GIT_COMMITTER_NAME": "Seed Bridge",
        "GIT_COMMITTER_EMAIL": "seed-git@localhost",
        "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
    }
    commit = _git_text(git_root, arguments, input_data=message, environment=identity)
    if commit is None or not _GIT_HEX.fullmatch(commit):
        raise SeedError("Git did not create an export commit")
    return commit.lower()


def _branch_is_checked_out(git_root: str | Path, branch_ref: str) -> bool:
    """Whether any linked worktree currently has the export branch checked out."""
    listing = _git_text(git_root, ["worktree", "list", "--porcelain"])
    assert listing is not None
    return any(line == "branch " + branch_ref for line in listing.splitlines())


def export_seed_revision(seed: Any, git_root: str | Path, revision: str,
                         *, branch: str = "seed/export") -> dict[str, str]:
    """Export a pinned Seed revision to an owned, one-way Git branch."""
    if not isinstance(revision, str) or not _SEED_ID.fullmatch(revision):
        raise SeedError("Seed export requires a full revision ID")
    branch_ref = _validate_branch(git_root, branch)
    if _branch_is_checked_out(git_root, branch_ref):
        raise SeedError("Seed export branch is checked out in a worktree")
    existing = _find_export(git_root, branch_ref, revision)
    if existing is not None:
        expected_tree = _write_tree(git_root, _seed_manifest(seed, revision))
        if _commit_tree(git_root, existing) != expected_tree:
            raise SeedError("Existing Seed export tree does not match the requested revision")
        mapping_ref = "refs/seed/exports/" + revision
        mapping = _read_ref(git_root, mapping_ref)
        if mapping is None:
            _update_ref(git_root, mapping_ref, existing, None)
        elif mapping != existing:
            raise SeedError("Seed export mapping points to another commit")
        return {"status": "already-exported", "revision": revision, "commit": existing, "branch": branch_ref}

    row = _seed_revision(seed, revision)
    parent_revision = row.get("parent")
    tip = _read_ref(git_root, branch_ref)
    if tip is not None and _exported_revision(git_root, tip) is None:
        raise SeedError("Dedicated export branch is not owned by Seed")
    if parent_revision is not None:
        parent_commit = _find_export(git_root, branch_ref, parent_revision)
        if parent_commit is not None and tip != parent_commit:
            raise SeedError("Dedicated branch does not match the Seed export parent")
        if parent_commit is None and tip is not None:
            raise SeedError("Export the Seed parent before appending to this branch")
    elif tip is not None:
        raise SeedError("Dedicated branch already has an incompatible export lineage")

    files = _seed_manifest(seed, revision)
    tree = _write_tree(git_root, files)
    commit = _bridge_commit(git_root, tree, tip, revision)
    _update_ref(git_root, branch_ref, commit, tip)
    mapping_ref = "refs/seed/exports/" + revision
    mapping = _read_ref(git_root, mapping_ref)
    if mapping is None:
        _update_ref(git_root, mapping_ref, commit, None)
    elif mapping != commit:
        raise SeedError("Seed export mapping points to another commit")
    return {"status": "exported", "revision": revision, "commit": commit, "branch": branch_ref}


def _target(arguments: argparse.Namespace) -> Any:
    if arguments.db:
        return Store(arguments.db)
    token = os.environ.get(arguments.token_env)
    if not token:
        raise SeedError(arguments.token_env + " must be set for HTTP Seed access")
    return Client(arguments.url, arguments.repo, token, allow_http=arguments.allow_http)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="existing local Seed SQLite database")
    parser.add_argument("--url", help="Seed service URL")
    parser.add_argument("--repo", help="Seed repository name for --url")
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--token-env", default="SEED_TOKEN")
    commands = parser.add_subparsers(dest="command", required=True)
    importing = commands.add_parser("import")
    importing.add_argument("--git", required=True, dest="git_root")
    importing.add_argument("--commit", required=True)
    exporting = commands.add_parser("export")
    exporting.add_argument("--git", required=True, dest="git_root")
    exporting.add_argument("--revision", required=True)
    exporting.add_argument("--branch", default="seed/export")
    arguments = parser.parse_args(argv)
    if bool(arguments.db) == bool(arguments.url):
        parser.error("use exactly one of --db or --url")
    if arguments.url and not arguments.repo:
        parser.error("--url requires --repo")
    try:
        target = _target(arguments)
        result = (
            import_git_commit(target, arguments.git_root, arguments.commit)
            if arguments.command == "import"
            else export_seed_revision(target, arguments.git_root, arguments.revision, branch=arguments.branch)
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except SeedError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}), file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
