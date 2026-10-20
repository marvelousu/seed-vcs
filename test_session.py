"""Focused contracts for the client-side revision-pinned work view."""

from io import BytesIO
import json
import unittest
from urllib.error import HTTPError

from seed import Conflict, SeedError, object_id
from seed_client import Client, Session


class MemoryClient:
    """Small pinned-store adapter for Session tests, without HTTP or disk."""

    def __init__(self, files):
        self.files = dict(files)
        self.proposals = []
        self.range_calls = []
        self.blobs = {
            object_id("blob", payload): payload for payload in self.files.values()
        }

    def read(self, path, revision):
        self.assert_base(revision)
        return self.files.get(path)

    def read_blob(self, identifier):
        return self.blobs[identifier]

    def range_snapshot(self, prefix, kind, revision):
        self.assert_base(revision)
        self.range_calls.append((prefix, kind, revision))
        selected = {
            path: object_id("blob", payload)
            for path, payload in self.files.items()
            if not prefix or path.startswith(prefix)
        }
        return {
            "revision": revision,
            "prefix": prefix,
            "kind": kind,
            "paths": sorted(selected),
            "files": selected,
            "fingerprint": "snapshot",
        }

    def propose(self, base, edits, **kwargs):
        self.assert_base(base)
        self.proposals.append((dict(edits), kwargs))
        return "proposal-1"

    @staticmethod
    def assert_base(revision):
        if revision != "base":
            raise AssertionError(f"unexpected revision {revision!r}")


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.client = MemoryClient(
            {
                "src/keep.txt": b"old\nneedle\n",
                "src/remove.txt": b"remove me\n",
                "src/plain.txt": b"ordinary\n",
                "docs/readme.txt": b"outside\n",
            }
        )
        self.session = Session(self.client, "base")

    def test_protocol_limits_fail_before_poisoning_a_task(self):
        for number in range(1000):
            self.session.write(f"generated/{number}", b"value")
            self.session.read(f"missing/{number}")
        with self.assertRaises(SeedError):
            self.session.write("one-too-many", b"value")
        with self.assertRaises(SeedError):
            self.session.read("another-missing")
        self.assertNotIn("one-too-many", self.session.edits)
        self.assertNotIn("another-missing", self.session.reads)
        self.session.write("generated/0", b"updated")
        self.assertIsNone(self.session.read("missing/0"))
        for number in range(64):
            self.session.files(f"scope{number}/")
        with self.assertRaises(SeedError):
            self.session.files("overflow/")
        self.assertNotIn(("membership", "overflow/"), self.session.ranges)
        self.session.propose()

    def test_read_view_overlays_edits_but_read_base_stays_pinned(self):
        self.session.write("src/keep.txt", b"new\n")
        self.session.write("src/remove.txt", None)
        self.session.write("src/new.txt", b"created\n")

        self.assertEqual(self.session.read("src/keep.txt"), b"new\n")
        self.assertEqual(self.session.read_base("src/keep.txt"), b"old\nneedle\n")
        self.assertIsNone(self.session.read("src/remove.txt"))
        self.assertEqual(self.session.read("src/new.txt"), b"created\n")
        self.assertEqual(
            self.session.files("src"), ["src/keep.txt", "src/new.txt", "src/plain.txt"]
        )
        self.assertEqual(self.session.ranges, {("membership", "src/")})
        self.assertEqual(
            self.session.reads, {"src/keep.txt", "src/remove.txt", "src/new.txt"}
        )

    def test_search_uses_overlay_and_guards_the_whole_subtree(self):
        self.session.write("src/plain.txt", b"now needle too\n")
        self.session.write("src/new.txt", b"needle created\n")
        self.session.write("src/remove.txt", None)

        result = self.session.search("needle", "src")

        self.assertEqual(
            result,
            {
                "matches": [
                    {"path": "src/keep.txt", "line": 2, "text": "needle"},
                    {"path": "src/new.txt", "line": 1, "text": "needle created"},
                    {"path": "src/plain.txt", "line": 1, "text": "now needle too"},
                ],
                "truncated": False,
            },
        )
        self.assertEqual(self.session.ranges, {("subtree", "src/")})
        self.assertEqual(self.client.range_calls, [("src/", "subtree", "base")])

    def test_search_has_a_bounded_result_without_weakening_its_guard(self):
        self.session.write("src/extra.txt", b"x\nx\nx\n")

        result = self.session.search("x", "src", limit=2)

        self.assertEqual(len(result["matches"]), 2)
        self.assertTrue(result["truncated"])
        self.assertIn(("subtree", "src/"), self.session.ranges)
        with self.assertRaises(SeedError):
            self.session.search("", "src")
        with self.assertRaises(SeedError):
            self.session.search("x", "src", limit=1001)

    def test_no_op_overlay_read_still_records_the_base_condition(self):
        self.session.write("src/keep.txt", b"old\nneedle\n")

        self.assertEqual(self.session.read("src/keep.txt"), b"old\nneedle\n")
        self.assertEqual(self.session.propose(supersedes="old-proposal"), "proposal-1")
        edits, arguments = self.client.proposals[0]
        self.assertEqual(edits["src/keep.txt"], b"old\nneedle\n")
        self.assertEqual(arguments["reads"], ["src/keep.txt"])
        self.assertEqual(arguments["ranges"], [])
        self.assertEqual(arguments["supersedes"], "old-proposal")

    def test_diff_is_json_safe_for_text_and_binary_changes(self):
        self.session.write("src/keep.txt", b"new\n")
        self.session.write("src/remove.txt", None)
        self.session.write("src/new.bin", b"\x00\xff")

        self.assertEqual(
            self.session.diff(),
            [
                {
                    "path": "src/keep.txt",
                    "before": "old\nneedle\n",
                    "after": "new\n",
                    "encoding": "utf-8",
                },
                {
                    "path": "src/new.bin",
                    "before": None,
                    "after": "AP8=",
                    "encoding": "base64",
                },
                {
                    "path": "src/remove.txt",
                    "before": "remove me\n",
                    "after": None,
                    "encoding": "utf-8",
                },
            ],
        )
        self.assertEqual(self.session.reads, set())


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return BytesIO(response)


class ClientProtocolTests(unittest.TestCase):
    def test_metrics_status_and_optional_proposal_fields(self):
        transport = Transport(
            [
                b'{"proposal":"first"}',
                b'{"proposal":"second"}',
                b'{"status":"pending"}',
            ]
        )
        client = Client("http://localhost", "repo", "token")
        client.opener = transport

        self.assertEqual(client.propose("base", {}), "first")
        self.assertEqual(
            client.propose(
                "base",
                {},
                ranges=[{"kind": "subtree", "prefix": "src"}],
                supersedes="prior",
            ),
            "second",
        )
        self.assertEqual(client.status("p" * 64), {"status": "pending"})
        first = json.loads(transport.requests[0].data)
        second = json.loads(transport.requests[1].data)
        self.assertNotIn("ranges", first)
        self.assertNotIn("supersedes", first)
        self.assertEqual(second["ranges"], [{"kind": "subtree", "prefix": "src/"}])
        self.assertEqual(second["supersedes"], "prior")
        self.assertEqual(client.metrics["requests"], 3)
        self.assertGreater(client.metrics["download_bytes"], 0)
        self.assertGreater(client.metrics["upload_bytes"], 0)
        previous = client.reset_metrics()
        self.assertEqual(previous["requests"], 3)
        self.assertEqual(client.metrics["requests"], 0)

    def test_conflict_keeps_structured_differences_and_head(self):
        payload = json.dumps(
            {
                "status": "conflict",
                "paths": {"price": {"kind": "content"}},
                "head": "h" * 64,
            }
        ).encode()
        error = HTTPError("http://localhost", 409, "conflict", {}, BytesIO(payload))
        client = Client("http://localhost", "repo", "token")
        client.opener = Transport([error])

        with self.assertRaises(Conflict) as failure:
            client.land("p" * 64)
        self.assertEqual(failure.exception.differences, {"price": {"kind": "content"}})
        self.assertEqual(failure.exception.head, "h" * 64)

    def test_range_snapshot_rejects_bad_kind_before_request(self):
        client = Client("http://localhost", "repo", "token")
        client.opener = Transport([])
        with self.assertRaises(SeedError):
            client.range_snapshot("src", "unknown")

    def test_old_writer_guard_is_a_protocol_error_instead_of_rework_conflict(self):
        payload = json.dumps({"status": "conflict", "paths": {
            "@seed/protocol-v2/": {"expected": {"requires": 2}, "actual": None}
        }}).encode()
        error = HTTPError("http://localhost", 409, "conflict", {}, BytesIO(payload))
        client = Client("http://localhost", "repo", "token")
        client.opener = Transport([error])
        with self.assertRaisesRegex(SeedError, "upgrade all service workers") as failure:
            client.land("p" * 64)
        self.assertNotIsInstance(failure.exception, Conflict)
        self.assertEqual(len(client.opener.requests), 1)

    def test_range_snapshot_rejects_a_response_from_another_revision(self):
        response = {
            "revision": "other-base",
            "prefix": "src/",
            "kind": "membership",
            "paths": [],
            "files": {},
            "fingerprint": "unused",
        }
        client = Client("http://localhost", "repo", "token")
        client.opener = Transport([json.dumps(response).encode()])

        with self.assertRaisesRegex(SeedError, "another revision"):
            client.range_snapshot("src", revision="requested-base")

    def test_blob_cache_has_entry_and_byte_limits_without_corrupting_bytes(self):
        first, second = b"\x00one", b"\xfftwo"
        client = Client("http://localhost", "repo", "token")
        client._BLOB_CACHE_SIZE = 2
        client._BLOB_CACHE_BYTES = 5
        client.opener = Transport([first, second, first])
        first_id = object_id("blob", first)
        second_id = object_id("blob", second)

        self.assertEqual(client.read_blob(first_id), first)
        self.assertEqual(client.read_blob(second_id), second)
        self.assertEqual(client._blob_cache_bytes, len(second))
        self.assertEqual(client.read_blob(first_id), first)
        self.assertEqual(len(client.opener.requests), 3)

        oversized = b"\x00oversized"
        other = Client("http://localhost", "repo", "token")
        other._BLOB_CACHE_BYTES = len(oversized) - 1
        other.opener = Transport([oversized, oversized])
        identifier = object_id("blob", oversized)
        self.assertEqual(other.read_blob(identifier), oversized)
        self.assertEqual(other.read_blob(identifier), oversized)
        self.assertEqual(other._blob_cache, {})
        self.assertEqual(len(other.opener.requests), 2)


if __name__ == "__main__":
    unittest.main()
