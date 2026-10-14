"""Standard-library HTTP client with revision-pinned automatic read capture."""

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from seed import Conflict, SeedError, file_path, object_id


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a repository's bearer credential to another URL.
        return None


class Client:
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
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    result = response.read()
                return result if raw else json.loads(result)
            except HTTPError as exc:
                try:
                    failure = json.loads(exc.read())
                except (ValueError, UnicodeError):
                    failure = {}
                if exc.code == 409 and failure.get("status") == "conflict":
                    raise Conflict(failure["paths"]) from exc
                if exc.code not in (429, 503) or attempt == 2:
                    raise SeedError(
                        f"Service request failed ({exc.code}): "
                        + str(
                            failure.get(
                                "message", failure.get("detail", "request rejected")
                            )
                        )
                    ) from exc
            except (URLError, TimeoutError, ConnectionError) as exc:
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

    def read(self, path, revision=None):
        query = {"path": file_path(path)}
        if revision:
            query["revision"] = revision
        state = self._request("GET", "/file?" + urlencode(query))
        identifier = state["blob"]
        if identifier is None:
            return None
        payload = self._request("GET", "/blobs/" + identifier, raw=True)
        if object_id("blob", payload) != identifier:
            raise SeedError("Service returned a damaged blob")
        return payload

    def propose(self, base, edits, *, reads=(), author="", message=""):
        writes = {
            file_path(path): None if payload is None else self.upload(payload)
            for path, payload in edits.items()
        }
        return self._request(
            "POST",
            "/proposals",
            {
                "base": base,
                "writes": writes,
                "reads": list(reads),
                "author": author,
                "message": message,
            },
        )["proposal"]

    def land(self, proposal):
        return self._request("POST", "/land", {"proposal": proposal})

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
    def __init__(self, client, base):
        self.client = client
        self.base = base
        self.reads = set()
        self.edits = {}

    def read(self, path):
        path = file_path(path)
        payload = self.client.read(path, self.base)
        self.reads.add(path)
        return payload

    def write(self, path, payload):
        if payload is not None and not isinstance(payload, bytes):
            raise SeedError("File contents must be bytes or None")
        self.edits[file_path(path)] = payload

    def propose(self, *, author="", message=""):
        return self.client.propose(
            self.base,
            self.edits,
            reads=sorted(self.reads),
            author=author,
            message=message,
        )
