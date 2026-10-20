"""Standard-library HTTP client and a revision-pinned agent work session."""

import base64
from collections import OrderedDict
import json
from http.client import IncompleteRead
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from seed import Conflict, SeedError, file_path, object_id, MAX_RANGES, V2_WRITER_GUARD


def _prefix(prefix):
    """Use the core's canonical range spelling without duplicating its rules."""
    from seed import normalize_prefix

    return normalize_prefix(prefix)


def _in_prefix(path, prefix):
    return not prefix or path.startswith(prefix)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a repository's bearer credential to another URL.
        return None


class Client:
    """HTTP client with verified immutable-blob caching and lightweight metrics."""

    _BLOB_CACHE_SIZE = 128
    _BLOB_CACHE_BYTES = 16 * 1024 * 1024

    def __init__(self, url, repository, token, *, timeout=30, allow_http=False):
        parsed = urlsplit(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use a server URL without credentials, query or fragment")
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and (local or allow_http)
        ):
            raise ValueError("Remote servers require HTTPS")
        import re

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", repository):
            raise ValueError("Invalid repository name")
        self.url = url.rstrip("/") + "/v1/repos/" + repository
        self.token = token
        self.timeout = timeout
        self.opener = build_opener(NoRedirects())
        self.uploaded = set()
        self._blob_cache = OrderedDict()
        self._blob_cache_bytes = 0
        self.metrics = {
            "requests": 0,
            "upload_bytes": 0,
            "download_bytes": 0,
            "request_seconds": 0.0,
        }

    def reset_metrics(self):
        """Reset counters and return their value before the reset."""
        previous = dict(self.metrics)
        self.metrics = {
            "requests": 0,
            "upload_bytes": 0,
            "download_bytes": 0,
            "request_seconds": 0.0,
        }
        return previous

    def _record_request(self, payload):
        self.metrics["requests"] += 1
        if payload is not None:
            self.metrics["upload_bytes"] += len(payload)

    def _record_response(self, payload, started):
        self.metrics["request_seconds"] += time.monotonic() - started
        self.metrics["download_bytes"] += len(payload)

    def _request(self, method, suffix="", body=None, *, raw=False):
        payload = (
            body
            if isinstance(body, bytes)
            else (json.dumps(body).encode() if body is not None else None)
        )
        headers = {"Authorization": "Bearer " + self.token}
        if payload is not None:
            headers["Content-Type"] = (
                "application/octet-stream"
                if isinstance(body, bytes)
                else "application/json"
            )
        request = Request(
            self.url + suffix, data=payload, headers=headers, method=method
        )
        for attempt in range(3):
            started = time.monotonic()
            self._record_request(payload)
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    result = response.read()
                self._record_response(result, started)
                return result if raw else json.loads(result)
            except HTTPError as exc:
                failure_bytes = exc.read()
                self._record_response(failure_bytes, started)
                try:
                    failure = json.loads(failure_bytes)
                except (ValueError, UnicodeError):
                    failure = {}
                if exc.code == 409 and failure.get("status") == "conflict":
                    differences = failure.get("paths", failure.get("differences", {}))
                    if V2_WRITER_GUARD in differences:
                        raise SeedError(
                            "Server writer does not support proposal protocol 2; "
                            "upgrade all service workers before retrying"
                        ) from exc
                    conflict = Conflict(differences)
                    # Older cores did not accept head in Conflict's constructor.  Keep
                    # the structured conflict intact while the protocol stays backward
                    # compatible with those servers.
                    if failure.get("head") is not None:
                        conflict.head = failure["head"]
                    raise conflict from exc
                if exc.code not in (429, 503) or attempt == 2:
                    raise SeedError(
                        f"Service request failed ({exc.code}): "
                        + str(
                            failure.get(
                                "message", failure.get("detail", "request rejected")
                            )
                        )
                    ) from exc
            except (URLError, TimeoutError, ConnectionError, IncompleteRead) as exc:
                self.metrics["request_seconds"] += time.monotonic() - started
                if attempt == 2:
                    raise SeedError(
                        "Service connection failed after three attempts"
                    ) from exc
            time.sleep(0.1 * (2**attempt))

    def create(self):
        return self._request("POST", "")

    def head(self):
        return self._request("GET", "/head")["revision"]

    def upload(self, payload):
        identifier = object_id("blob", payload)
        if identifier not in self.uploaded:
            self._request("PUT", "/blobs/" + identifier, payload)
            self.uploaded.add(identifier)
        return identifier

    def read_blob(self, identifier):
        """Fetch an immutable blob once and verify it before caching it."""
        cached = self._blob_cache.get(identifier)
        if cached is not None:
            self._blob_cache.move_to_end(identifier)
            return cached
        payload = self._request("GET", "/blobs/" + identifier, raw=True)
        if object_id("blob", payload) != identifier:
            raise SeedError("Service returned a damaged blob")
        # A content-addressed object can legitimately be large.  Serve such
        # an object, but do not let a small entry count turn into an unbounded
        # process memory commitment.
        if len(payload) <= self._BLOB_CACHE_BYTES:
            while self._blob_cache and (
                len(self._blob_cache) >= self._BLOB_CACHE_SIZE
                or self._blob_cache_bytes + len(payload) > self._BLOB_CACHE_BYTES
            ):
                _, evicted = self._blob_cache.popitem(last=False)
                self._blob_cache_bytes -= len(evicted)
            self._blob_cache[identifier] = payload
            self._blob_cache_bytes += len(payload)
        return payload

    def read(self, path, revision=None):
        query = {"path": file_path(path)}
        if revision:
            query["revision"] = revision
        state = self._request("GET", "/file?" + urlencode(query))
        identifier = state["blob"]
        if identifier is None:
            return None
        return self.read_blob(identifier)

    def range_snapshot(self, prefix="", kind="membership", revision=None):
        """Return one complete, pinned range snapshot; never synthesize pages."""
        prefix = _prefix(prefix)
        if kind not in ("membership", "subtree"):
            raise SeedError("Range kind must be 'membership' or 'subtree'")
        query = {"prefix": prefix, "kind": kind}
        if revision:
            query["revision"] = revision
        result = self._request("GET", "/range?" + urlencode(query))
        if result.get("prefix") != prefix or result.get("kind") != kind:
            raise SeedError("Service returned a mismatched range snapshot")
        if revision is not None and result.get("revision") != revision:
            raise SeedError("Service returned a range snapshot from another revision")
        paths = result.get("paths")
        files = result.get("files")
        if not isinstance(paths, list) or not isinstance(files, dict):
            raise SeedError("Service returned an invalid range snapshot")
        if (
            paths != sorted(paths)
            or len(paths) != len(set(paths))
            or set(paths) != set(files)
        ):
            raise SeedError("Service returned an incomplete range snapshot")
        return result

    def propose(
        self,
        base,
        edits,
        *,
        reads=(),
        ranges=(),
        supersedes=None,
        author="",
        message="",
    ):
        writes = {
            file_path(path): None if payload is None else self.upload(payload)
            for path, payload in edits.items()
        }
        body = {
            "base": base,
            "writes": writes,
            "reads": list(reads),
            "author": author,
            "message": message,
        }
        normalized_ranges = {
            (descriptor["kind"], _prefix(descriptor["prefix"]))
            for descriptor in ranges
        }
        if normalized_ranges:
            for kind, _ in normalized_ranges:
                if kind not in ("membership", "subtree"):
                    raise SeedError("Range kind must be 'membership' or 'subtree'")
            body["ranges"] = [
                {"kind": kind, "prefix": prefix}
                for kind, prefix in sorted(normalized_ranges)
            ]
        if supersedes is not None:
            body["supersedes"] = supersedes
        return self._request("POST", "/proposals", body)["proposal"]

    def describe(self, proposal):
        return self._request("GET", "/proposals/" + proposal)

    def status(self, proposal):
        return self._request("GET", "/proposals/" + proposal + "/status")

    def land(self, proposal):
        return self._request("POST", "/land", {"proposal": proposal})

    def create_candidate(self, proposals):
        return self._request("POST", "/candidates", {"proposals": list(proposals)})

    def candidate(self, identifier):
        from seed_validation import identifier as validate_id
        return self._request("GET", "/candidates/" + validate_id(identifier))

    def publish_candidate(self, identifier):
        from seed_validation import identifier as validate_id
        return self._request("POST", "/candidates/" + validate_id(identifier) + "/publish", {})

    def set_validation_policy(self, configurations):
        return self._request("PUT", "/validation-policy", {"configurations": list(configurations)})

    def record_validation(self, **report):
        return self._request("POST", "/validations", report)

    def validation_state(self):
        return self._request("GET", "/validation-state")

    def promote(self, revision):
        return self._request("POST", "/promotions", {"revision": revision})

    def land_many(self, proposals):
        return self._request("POST", "/land-batch", {"proposals": list(proposals)})[
            "results"
        ]

    def files(self, *, revision=None, after="", limit=100):
        query = {"after": after, "limit": limit}
        if revision:
            query["revision"] = revision
        return self._request("GET", "/files?" + urlencode(query))

    def history(self, *, after=-1, limit=100):
        return self._request(
            "GET", "/history?" + urlencode({"after": after, "limit": limit})
        )

    def begin(self):
        return Session(self, self.head())


class Session:
    """A pinned base revision plus the caller's not-yet-submitted overlay."""

    _SEARCH_LIMIT = 1000

    def __init__(self, client, base):
        self.client = client
        self.base = base
        self.reads = set()
        self.ranges = set()
        self.edits = {}
        self._base_reads = {}
        self._range_snapshots = {}

    def _base_read(self, path):
        if path not in self._base_reads:
            self._base_reads[path] = self.client.read(path, self.base)
        return self._base_reads[path]

    def _remember_read(self, path):
        if path not in self.reads and len(self.reads) >= 1000:
            raise SeedError("A task supports at most 1000 explicit reads; use a declared subtree for larger inputs")
        self.reads.add(path)

    def read_base(self, path):
        """Read the unmodified pinned base and retain its condition."""
        path = file_path(path)
        self._remember_read(path)
        return self._base_read(path)

    def read(self, path):
        """Read the current work view, including additions, edits, and deletes."""
        path = file_path(path)
        # Retaining this condition even for an overlay/no-op read is conservative:
        # a derived output is still tied to the revision from which work began.
        self._remember_read(path)
        if path in self.edits:
            return self.edits[path]
        return self._base_read(path)

    def write(self, path, payload):
        if payload is not None and not isinstance(payload, bytes):
            raise SeedError("File contents must be bytes or None")
        path = file_path(path)
        if path not in self.edits and len(self.edits) >= 1000:
            raise SeedError("A task supports at most 1000 pending write paths; split the work into proposals")
        self.edits[path] = payload

    def track_scope(self, prefix="", kind="subtree"):
        """Record a pinned base range as a proposal condition and return it."""
        prefix = _prefix(prefix)
        if kind not in ("membership", "subtree"):
            raise SeedError("Range kind must be 'membership' or 'subtree'")
        key = (kind, prefix)
        if key not in self.ranges and len(self.ranges) >= MAX_RANGES:
            raise SeedError(f"A task supports at most {MAX_RANGES} range conditions; begin a separately scoped task")
        if key not in self._range_snapshots:
            snapshot = self.client.range_snapshot(
                prefix, kind, self.base
            )
            if snapshot.get("revision") != self.base:
                raise SeedError("Range snapshot does not match the session base")
            self._range_snapshots[key] = snapshot
        self.ranges.add(key)
        return self._range_snapshots[key]

    def _base_files(self, prefix, kind):
        snapshot = self.track_scope(prefix, kind)
        files = snapshot.get("files")
        if not isinstance(files, dict):
            raise SeedError("Service returned an invalid range snapshot")
        return files

    def files(self, prefix=""):
        """List paths in the current overlay while guarding base membership."""
        prefix = _prefix(prefix)
        paths = set(self._base_files(prefix, "membership"))
        for path, payload in self.edits.items():
            if not _in_prefix(path, prefix):
                continue
            if payload is None:
                paths.discard(path)
            else:
                paths.add(path)
        return sorted(paths)

    def _snapshot_payload(self, path, files):
        if path in self.edits:
            return self.edits[path]
        identifier = files[path]
        if identifier is None:
            return None
        return self.client.read_blob(identifier)

    def search(self, query, prefix="", limit=100):
        """Case-sensitive literal UTF-8 line search over the current work view."""
        if not isinstance(query, str) or not query:
            raise SeedError("Search query must be a non-empty string")
        if not isinstance(limit, int) or not 1 <= limit <= self._SEARCH_LIMIT:
            raise SeedError(f"Search limit must be between 1 and {self._SEARCH_LIMIT}")
        prefix = _prefix(prefix)
        # A subtree condition covers paths which were not matches at base and may
        # later become matches, as well as edits to existing non-matching files.
        files = self._base_files(prefix, "subtree")
        paths = set(files)
        paths.update(
            path for path, payload in self.edits.items()
            if payload is not None and _in_prefix(path, prefix)
        )
        matches = []
        truncated = False
        for path in sorted(paths):
            payload = self._snapshot_payload(path, files)
            if payload is None:
                continue
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if query not in line:
                    continue
                if len(matches) >= limit:
                    truncated = True
                    break
                matches.append({"path": path, "line": line_number, "text": line})
            if truncated:
                break
        return {"matches": matches, "truncated": truncated}

    @staticmethod
    def _diff_value(payloads):
        before, after = payloads
        try:
            return {
                "before": None if before is None else before.decode("utf-8"),
                "after": None if after is None else after.decode("utf-8"),
                "encoding": "utf-8",
            }
        except UnicodeDecodeError:
            return {
                "before": (
                    None if before is None else base64.b64encode(before).decode("ascii")
                ),
                "after": (
                    None if after is None else base64.b64encode(after).decode("ascii")
                ),
                "encoding": "base64",
            }

    def diff(self):
        """Return changed paths as JSON-safe base/proposed content pairs."""
        changes = []
        for path in sorted(self.edits):
            # A write's base value is already an implicit condition.  Rendering
            # a diff should not quietly turn it into an additional read guard.
            before = self._base_read(path)
            after = self.edits[path]
            if before == after:
                continue
            changes.append({"path": path, **self._diff_value((before, after))})
        return changes

    def propose(self, *, author="", message="", supersedes=None):
        return self.client.propose(
            self.base,
            self.edits,
            reads=sorted(self.reads),
            ranges=[
                {"kind": kind, "prefix": prefix}
                for kind, prefix in sorted(self.ranges)
            ],
            supersedes=supersedes,
            author=author,
            message=message,
        )
