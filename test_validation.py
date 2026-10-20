"""Validation binds exact revisions; promotion and writer permissions differ."""
import os
from pathlib import Path
import sys
import unittest

from seed import Conflict, SeedError, object_id
from seed_validation import configuration_id, run_validation
import test_service


class ValidationTests(unittest.TestCase):
    dsn = ""
    setUp = test_service.ServiceTests.setUp
    tearDown = test_service.ServiceTests.tearDown
    client_for = test_service.ServiceTests.client_for
    commit = test_service.ServiceTests.commit

    def clients(self):
        return self.client_for(self.repo, self.validator), self.client_for(self.repo, self.operator)

    def report(self, client, subject, config="1" * 64, passed=True, request="attempt-1", kind="revision"):
        return client.record_validation(kind=kind, subject=subject, configuration=config,
                                        environment="2" * 64, passed=passed,
                                        returncode=0 if passed else 1,
                                        log_digest="3" * 64, request=request)

    def test_candidate_is_private_until_validated_atomic_publication_and_retry(self):
        validator, operator = self.clients()
        base = self.commit({"a": b"old", "b": b"old"})
        first = self.client.propose(base, {"a": b"one"})
        second = self.client.propose(base, {"b": b"two"})
        candidate = self.client.create_candidate([first, second])
        self.assertEqual(self.client.head(), base)
        self.assertEqual(self.client.status(first)["status"], "pending")
        self.assertEqual(candidate["files"]["a"], object_id("blob", b"one"))
        with self.assertRaises(SeedError):
            self.client.publish_candidate(candidate["candidate"])
        operator.set_validation_policy(["1" * 64])
        self.report(validator, candidate["candidate"], kind="candidate")
        self.assertIsNone(self.client.validation_state()["verified"])
        published = self.client.publish_candidate(candidate["candidate"])
        self.assertEqual(published["revision"], candidate["revision"])
        self.assertEqual(self.client.read("a"), b"one")
        self.assertEqual(self.client.read("b"), b"two")
        state = self.client.validation_state()
        self.assertEqual(state["verified"]["revision"], candidate["revision"])
        self.assertIsNone(state["deployable"])
        operator.promote(candidate["revision"])
        self.commit({"later": b"independent"})
        self.assertEqual(self.client.publish_candidate(candidate["candidate"])["status"], "already-landed")
        self.assertEqual(self.client.validation_state()["deployable"]["revision"], candidate["revision"])

    def test_failed_candidate_preview_rolls_back_earlier_proposals(self):
        base = self.commit({"a": b"old", "b": b"old"})
        first = self.client.propose(base, {"a": b"candidate"})
        second = self.client.propose(base, {"b": b"candidate"})
        current = self.commit({"b": b"background"})
        with self.assertRaises(Conflict):
            self.client.create_candidate([first, second])
        self.assertEqual(self.client.head(), current)
        self.assertEqual(self.client.read("a"), b"old")
        self.assertEqual(self.client.status(first)["status"], "pending")

    def test_candidate_manifest_requires_read_as_well_as_write_permission(self):
        base = self.commit({"private-name": b"content"})
        proposal = self.client.propose(base, {"result": b"new"})
        writer = self.client_for(self.repo, self.write_only)
        with self.assertRaisesRegex(SeedError, "403"):
            writer.create_candidate([proposal])
        self.assertEqual(self.client.head(), base)

    def test_writer_cannot_forge_validation_or_promote_or_change_policy(self):
        validator, operator = self.clients()
        revision = self.commit({"a": b"one"})
        for action in (lambda: self.report(self.client, revision),
                       lambda: self.client.set_validation_policy(["1" * 64]),
                       lambda: self.client.promote(revision),
                       lambda: validator.promote(revision),
                       lambda: operator.record_validation(kind="revision", subject=revision,
                           configuration="1" * 64, environment="2" * 64, passed=True,
                           returncode=0, log_digest="3" * 64, request="forged")):
            with self.assertRaisesRegex(SeedError, "403"):
                action()
        self.assertIsNone(self.client.validation_state()["deployable"])

    def test_failed_new_head_keeps_prior_deployable_and_policy_change_invalidates(self):
        validator, operator = self.clients()
        good = self.commit({"a": b"good"})
        operator.set_validation_policy(["1" * 64, "4" * 64])
        self.report(validator, good)
        self.assertIsNone(self.client.validation_state()["verified"])
        self.report(validator, good, config="4" * 64)
        operator.promote(good)
        bad = self.commit({"a": b"bad"})
        self.report(validator, bad, passed=False)
        state = self.client.validation_state()
        self.assertEqual(state["head"], bad)
        self.assertEqual(state["verified"]["revision"], good)
        self.assertEqual(state["deployable"]["revision"], good)
        with self.assertRaises(SeedError):
            operator.promote(bad)
        operator.set_validation_policy(["5" * 64])
        self.assertIsNone(self.client.validation_state()["deployable"])
        self.report(validator, good, config="5" * 64)
        self.assertEqual(self.client.validation_state()["verified"]["revision"], good)

    def test_later_failure_cannot_be_undone_by_retrying_old_success_report(self):
        validator, operator = self.clients()
        revision = self.commit({"a": b"one"})
        operator.set_validation_policy(["1" * 64])
        success = self.report(validator, revision)
        operator.promote(revision)
        self.report(validator, revision, passed=False, request="attempt-2")
        self.assertEqual(self.report(validator, revision), success)
        state = self.client.validation_state()
        self.assertIsNone(state["verified"])
        self.assertIsNone(state["deployable"])
        self.assertEqual(state["head"], revision)

    def test_head_advance_after_test_invalidates_strict_candidate(self):
        validator, operator = self.clients()
        base = self.commit({"a": b"one"})
        proposal = self.client.propose(base, {"a": b"two"})
        candidate = self.client.create_candidate([proposal])
        operator.set_validation_policy(["1" * 64])
        self.report(validator, candidate["candidate"], kind="candidate")
        newer = self.commit({"unrelated": b"later"})
        with self.assertRaises(Conflict) as failure:
            self.client.publish_candidate(candidate["candidate"])
        self.assertIn("@head", failure.exception.differences)
        self.assertEqual(failure.exception.head, newer)
        self.assertEqual(self.client.read("a"), b"one")
        self.assertIsNone(self.client.validation_state()["verified"])

    def test_combined_candidate_fails_real_test_and_cannot_publish(self):
        validator, operator = self.clients()
        base = self.commit({"a.py": b"VALUE = 1\n", "b.py": b"VALUE = 1\n",
                            "test_sum.py": b"import unittest, a, b\nclass Check(unittest.TestCase):\n def test_sum(self): self.assertLessEqual(a.VALUE + b.VALUE, 3)\n"})
        command = [sys.executable, "-B", "-m", "unittest", "-v"]
        config = configuration_id(command, "test-environment-v1")
        operator.set_validation_policy([config])
        good = run_validation(validator, kind="revision", subject=base,
                              workspace=str(self.root / "good-work"), command=command,
                              environment="test-environment-v1")
        self.assertTrue(good["validation"]["report"]["passed"])
        operator.promote(base)
        left = self.client.propose(base, {"a.py": b"VALUE = 2\n"})
        right = self.client.propose(base, {"b.py": b"VALUE = 2\n"})
        candidate = self.client.create_candidate([left, right])
        result = run_validation(validator, kind="candidate", subject=candidate["candidate"],
                                workspace=str(self.root / "combined-work"), command=command,
                                environment="test-environment-v1")
        self.assertFalse(result["validation"]["report"]["passed"])
        self.assertEqual(result["result"]["returncode"], 1)
        with self.assertRaises(SeedError):
            self.client.publish_candidate(candidate["candidate"])
        self.assertEqual(self.client.head(), base)
        self.assertEqual(self.client.validation_state()["deployable"]["revision"], base)

    def test_modifying_versioned_inputs_during_test_is_not_a_pass(self):
        validator, operator = self.clients()
        base = self.commit({"input.txt": b"original"})
        command = [sys.executable, "-c", "from pathlib import Path; Path('input.txt').write_text('changed')"]
        result = run_validation(validator, kind="revision", subject=base,
                                workspace=str(self.root / "mutated-work"), command=command,
                                environment="test-environment-v1")
        self.assertEqual(result["result"]["returncode"], 0)
        self.assertEqual(result["modified_inputs"], ["input.txt"])
        self.assertFalse(result["validation"]["report"]["passed"])


@unittest.skipUnless(os.environ.get("SEED_TEST_DATABASE_URL"), "Set SEED_TEST_DATABASE_URL for PostgreSQL")
class PostgresValidationTests(ValidationTests):
    dsn = os.environ.get("SEED_TEST_DATABASE_URL", "")


if __name__ == "__main__":
    unittest.main()
