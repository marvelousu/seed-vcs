"""Service contracts exercised through HTTP, with an optional real PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
from io import BytesIO
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from urllib.error import HTTPError, URLError

from fastapi.testclient import TestClient

from seed import Conflict, SeedError
from seed_client import Client
from seed_server import Auth, BODY_LIMIT, create_app, issue_token


ROOT = Path(__file__).resolve().parent


class Transport:
    def __init__(self, http):
        self.http = http

    def open(self, request, timeout=None):
        response = self.http.request(
            request.method,
            request.full_url,
            content=request.data,
            headers=dict(request.header_items()),
        )
        stream = BytesIO(response.content)
        if response.status_code >= 400:
            raise HTTPError(
                request.full_url,
                response.status_code,
                "rejected",
                response.headers,
                stream,
            )
        return stream


class ServiceTests(unittest.TestCase):
    dsn = ""

    def setUp(self):
        (ROOT / "artifacts/tmp").mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "artifacts/tmp")
        self.root = Path(self.temporary.name)
        self.repo = "test_" + secrets.token_hex(8)
        self.other = "test_" + secrets.token_hex(8)
        self.token = secrets.token_urlsafe(32)
        self.reader = secrets.token_urlsafe(32)
        self.validator = secrets.token_urlsafe(32)
        self.operator = secrets.token_urlsafe(32)
        self.write_only = secrets.token_urlsafe(32)
        auth = Auth.from_entries(
            [
                {
                    "sha256": hashlib.sha256(self.token.encode()).hexdigest(),
                    "repositories": [self.repo, self.other],
                    "permissions": ["read", "write", "create"],
                },
                {
                    "sha256": hashlib.sha256(self.reader.encode()).hexdigest(),
                    "repositories": [self.repo],
                    "permissions": ["read"],
                },
                {"sha256": hashlib.sha256(self.write_only.encode()).hexdigest(),
                 "repositories": [self.repo], "permissions": ["write"]},
                {"sha256": hashlib.sha256(self.validator.encode()).hexdigest(),
                 "repositories": [self.repo], "permissions": ["read", "validate"]},
                {"sha256": hashlib.sha256(self.operator.encode()).hexdigest(),
                 "repositories": [self.repo], "permissions": ["read", "promote"]},
            ]
        )
        self.app = create_app(root=self.root, auth=auth, dsn=self.dsn)
        self.http = TestClient(self.app)
        self.http.__enter__()
        self.client = self.client_for(self.repo, self.token)
        self.client.create()

    def tearDown(self):
        if self.dsn:
            provider = self.app.state.provider
            with provider.pool.connection() as connection:
                for store in provider.stores.values():
                    # Only schemas created by this isolated test, never supplied names.
                    connection.execute(f'DROP SCHEMA "{store.schema}" CASCADE')
        self.http.__exit__(None, None, None)
        self.temporary.cleanup()

    def client_for(self, repo, token):
        client = Client("http://localhost", repo, token)
        client.opener = Transport(self.http)
        return client

    def commit(self, edits, **kwargs):
        proposal = self.client.propose(self.client.head(), edits, **kwargs)
        return self.client.land(proposal)["revision"]

    def test_automatic_reads_pin_the_base_and_detect_changed_dependency(self):
        base = self.commit({"price": b"700"})
        session = self.client.begin()
        self.assertEqual(session.read("price"), b"700")
        self.assertIsNone(session.read("tax"))
        self.commit({"price": b"800"})
        self.assertEqual(session.read("price"), b"700")
        session.write("total", b"770")
        proposal = session.propose()
        with self.assertRaises(Conflict) as failure:
            self.client.land(proposal)
        self.assertEqual(set(failure.exception.differences), {"price"})
        self.assertIsNone(self.client.read("total"))
        self.assertEqual(session.base, base)

    def test_read_absence_is_automatically_captured(self):
        session = self.client.begin()
        self.assertIsNone(session.read("config"))
        session.write("output", b"default")
        proposal = session.propose()
        self.commit({"config": b"custom"})
        with self.assertRaises(Conflict):
            self.client.land(proposal)

    def test_working_view_tracks_membership_and_outside_changes(self):
        self.commit({"items/a": b"10", "docs/note": b"old"})
        session = self.client.begin()
        self.assertEqual(session.files("items"), ["items/a"])
        session.write("items/local", b"5")
        self.assertEqual(session.read("items/local"), b"5")
        self.assertEqual(session.files("items/"), ["items/a", "items/local"])
        self.commit({"docs/note": b"new"})
        proposal = session.propose()
        self.assertEqual(self.client.land(proposal)["status"], "landed")

        stale = self.client.begin()
        stale.files("items/")
        stale.write("total", b"15")
        proposal = stale.propose()
        self.commit({"items/remote": b"20"})
        with self.assertRaises(Conflict) as failure:
            self.client.land(proposal)
        self.assertEqual(failure.exception.head, self.client.head())
        self.assertTrue(any(value.get("kind") == "membership"
                            for value in failure.exception.differences.values()))
        self.assertIsNone(self.client.read("total"))

    def test_search_catches_new_matches_even_when_results_were_empty(self):
        self.commit({"src/code": b"nothing here"})
        task = self.client.begin()
        self.assertEqual(task.search("TODO", "src/")["matches"], [])
        task.write("report", b"none")
        proposal = task.propose()
        self.commit({"src/new": b"TODO fix"})
        with self.assertRaises(Conflict):
            self.client.land(proposal)
        self.assertIsNone(self.client.read("report"))

    def test_status_and_rework_lineage_survive_successful_retry(self):
        self.commit({"price": b"700"})
        first = self.client.begin()
        first.read("price")
        first.write("total", b"770")
        proposal = first.propose()
        self.assertEqual(self.client.status(proposal)["status"], "pending")
        self.commit({"price": b"800"})
        with self.assertRaises(Conflict):
            self.client.land(proposal)
        retry = self.client.begin()
        price = int(retry.read("price"))
        retry.write("total", str(price * 110 // 100).encode())
        replacement = retry.propose(supersedes=proposal)
        self.assertNotEqual(replacement, proposal)
        self.assertEqual(self.client.describe(replacement)["supersedes"], proposal)
        result = self.client.land(replacement)
        self.assertEqual(self.client.status(replacement)["revision"], result["revision"])
        self.assertEqual(self.client.land(replacement)["status"], "already-landed")
        self.assertEqual(self.client.read("total"), b"880")

    def test_range_endpoints_validate_scope_and_permissions(self):
        headers = {"Authorization": "Bearer " + self.token}
        endpoint = f"/v1/repos/{self.repo}/range"
        self.assertEqual(self.http.get(endpoint).status_code, 401)
        for query in ({"prefix": "../escape"}, {"kind": "unknown"}):
            self.assertEqual(self.http.get(endpoint, params=query,
                                           headers=headers).status_code, 422)
        self.assertEqual(self.http.get(endpoint, headers=headers).status_code, 200)

    def test_unicode_range_and_legacy_writer_guard_after_provider_reopen(self):
        from seed import V2_WRITER_GUARD

        base = self.commit({"資料/要約": b"original", "資料別/要約": b"outside"})
        task = self.client.begin()
        self.assertEqual(task.files("資料/"), ["資料/要約"])
        task.write("result", b"derived")
        proposal = task.propose()
        store = self.app.state.provider.repository(self.repo)
        body = self.client.describe(proposal)
        with store._db() as db:
            # The old writer would compare this invalid-for-a-file expected
            # value and reject instead of ignoring format-2 dependencies.
            self.assertIsNone(store._current_many(db, body["expected"])[V2_WRITER_GUARD])
        self.assertEqual(body["expected"][V2_WRITER_GUARD], {"requires": 2})
        self.app.state.provider.stores.pop(self.repo)
        self.assertEqual(self.client.create()["revision"], base)
        self.commit({"資料/追加": b"new"})
        with self.assertRaises(Conflict):
            self.client.land(proposal)
        self.assertEqual(self.client.range_snapshot("資料/", revision=base)["paths"],
                         ["資料/要約"])

    def test_batch_isolates_conflict_and_namespace_failure(self):
        base = self.commit({"a": b"0", "pkg": b"file"})
        first = self.client.propose(base, {"a": b"1"})
        conflict = self.client.propose(base, {"a": b"2", "partial": b"no"})
        invalid = self.client.propose(base, {"pkg/child": b"no", "partial2": b"no"})
        last = self.client.propose(base, {"b": b"ok"})
        result = self.client.land_many([first, conflict, invalid, last, first])
        self.assertEqual(
            [r["status"] for r in result],
            ["landed", "conflict", "error", "landed", "already-landed"],
        )
        self.assertIsNone(self.client.read("partial"))
        self.assertIsNone(self.client.read("partial2"))
        self.assertEqual(self.client.read("pkg"), b"file")
        self.assertEqual(self.client.read("b"), b"ok")

    def test_concurrent_independent_submissions_keep_every_update(self):
        base = self.client.head()

        def submit(i):
            client = self.client_for(self.repo, self.token)
            proposal = client.propose(base, {f"agents/{i}": str(i).encode()})
            return client.land(proposal)

        with ThreadPoolExecutor(max_workers=16) as pool:
            result = list(pool.map(submit, range(48)))
        self.assertTrue(all(r["status"] == "landed" for r in result))
        self.assertEqual(len(self.client.files(limit=100)["files"]), 48)

    def test_concurrent_same_proposal_creates_one_revision(self):
        proposal = self.client.propose(self.client.head(), {"file": b"same"})
        with ThreadPoolExecutor(max_workers=12) as pool:
            result = list(pool.map(lambda _: self.client.land(proposal), range(24)))
        self.assertEqual(sum(r["status"] == "landed" for r in result), 1)
        self.assertEqual(len({r["revision"] for r in result}), 1)
        self.assertEqual(len(self.client.history()["revisions"]), 2)

    def test_binary_data_empty_data_and_deletion_roundtrip(self):
        first = self.commit({"binary": b"\0\xff", "empty": b""})
        self.commit({"binary": None})
        self.assertEqual(self.client.read("binary", first), b"\0\xff")
        self.assertIsNone(self.client.read("binary"))
        self.assertEqual(self.client.read("empty"), b"")

    def test_file_pages_remain_on_a_revision_and_skip_tombstones(self):
        first = self.commit({f"files/{i:03}": b"data" for i in range(25)})
        self.commit({"files/002": None, "files/999": b"later"})
        paths = []
        cursor = ""
        while True:
            page = self.client.files(revision=first, after=cursor, limit=7)
            self.assertLessEqual(len(page["files"]), 7)
            self.assertEqual(page["revision"], first)
            paths.extend(page["files"])
            if page["next"] is None:
                break
            cursor = page["next"]
        self.assertEqual(paths, [f"files/{i:03}" for i in range(25)])
        current = self.client.files(limit=100)["files"]
        self.assertNotIn("files/002", current)
        self.assertIn("files/999", current)

    def test_history_pages_have_no_duplicates(self):
        for i in range(6):
            self.commit({"file": str(i).encode()})
        rows, cursor = [], -1
        while True:
            page = self.client.history(after=cursor, limit=2)
            rows.extend(page["revisions"])
            if page["next"] is None:
                break
            cursor = page["next"]
        self.assertEqual([row["seq"] for row in rows], list(range(7)))

    def test_authentication_and_repository_scope_precede_storage(self):
        path = f"/v1/repos/{self.repo}/head"
        self.assertEqual(self.http.get(path).status_code, 401)
        self.assertEqual(
            self.http.get(path, headers={"Authorization": "Bearer wrong"}).status_code,
            401,
        )
        reader = self.client_for(self.repo, self.reader)
        self.assertEqual(reader.head(), self.client.head())
        with self.assertRaises(SeedError):
            reader.propose(reader.head(), {"denied": b"no"})
        other_reader = self.client_for(self.other, self.reader)
        with self.assertRaises(SeedError) as failure:
            other_reader.head()
        self.assertIn("403", str(failure.exception))
        other = self.client_for(self.other, self.token)
        other.create()
        blob = self.client.upload(b"private bytes")
        with self.assertRaises(SeedError):
            other._request("GET", "/blobs/" + blob)

    def test_invalid_inputs_and_body_limits(self):
        headers = {"Authorization": "Bearer " + self.token}
        prefix = f"/v1/repos/{self.repo}"
        self.assertEqual(
            self.http.put(
                prefix + "/blobs/" + "0" * 64, content=b"wrong digest", headers=headers
            ).status_code,
            422,
        )
        self.assertEqual(
            self.http.post(
                prefix + "/proposals",
                json={"base": self.client.head(), "writes": {"../escape": None}},
                headers=headers,
            ).status_code,
            422,
        )
        self.assertEqual(
            self.http.get(prefix + "/files?limit=1001", headers=headers).status_code,
            422,
        )
        oversized = {**headers, "Content-Length": str(BODY_LIMIT + 1)}
        self.assertEqual(
            self.http.put(
                prefix + "/blobs/" + "0" * 64, content=b"x", headers=oversized
            ).status_code,
            413,
        )
        # The anonymous oversized request is refused before its body is consumed.
        self.assertEqual(
            self.http.put(
                prefix + "/blobs/" + "0" * 64,
                content=b"x",
                headers={"Content-Length": str(BODY_LIMIT + 1)},
            ).status_code,
            401,
        )

    def test_retry_after_head_advances_returns_the_original_revision(self):
        proposal = self.client.propose(self.client.head(), {"file": b"value"})
        first = self.client.land(proposal)
        head = self.commit({"other": b"later"})
        retry = self.client.land(proposal)
        self.assertEqual(
            retry, {"status": "already-landed", "revision": first["revision"]}
        )
        self.assertEqual(self.client.head(), head)

    def test_external_http_requires_explicit_opt_in(self):
        with self.assertRaises(ValueError):
            Client("http://example.com", "repo", "token")
        with self.assertRaises(ValueError):
            Client("https://user:password@example.com", "repo", "token")

    def test_lost_landing_response_is_retried_without_a_duplicate(self):
        proposal = self.client.propose(self.client.head(), {"file": b"value"})
        underlying = self.client.opener

        class LostResponse:
            dropped = False

            def open(self, request, timeout=None):
                result = underlying.open(request, timeout)
                if not self.dropped and request.full_url.endswith("/land"):
                    self.dropped = True
                    result.close()
                    raise URLError("simulated response loss after commit")
                return result

        self.client.opener = LostResponse()
        result = self.client.land(proposal)
        self.assertEqual(result["status"], "already-landed")
        self.assertEqual(len(self.client.history()["revisions"]), 2)

    def test_chunked_body_limit_is_enforced_without_content_length(self):
        headers = {"Authorization": "Bearer " + self.token}
        response = self.http.put(
            f"/v1/repos/{self.repo}/blobs/" + "0" * 64,
            content=iter([b"x" * (BODY_LIMIT // 2)] * 3),
            headers=headers,
        )
        self.assertEqual(response.status_code, 413)

    def test_credential_issuance_preserves_concurrent_grants(self):
        path = self.root / "test-credentials.json"
        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(
                pool.map(lambda _: issue_token(path, self.repo, ["read"]), range(16))
            )
        config = path.read_text()
        for token in tokens:
            self.assertNotIn(token, config)
            Auth(path).check("Bearer " + token, self.repo, "read")


@unittest.skipUnless(
    os.environ.get("SEED_TEST_DATABASE_URL"),
    "Set SEED_TEST_DATABASE_URL for PostgreSQL",
)
class PostgresServiceTests(ServiceTests):
    dsn = os.environ.get("SEED_TEST_DATABASE_URL", "")


if __name__ == "__main__":
    unittest.main()
