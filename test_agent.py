"""Focused restart and safe-workspace tests for the JSON agent adapter."""

from __future__ import annotations

import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from seed import Conflict, SeedError
from seed import object_id
import seed_agent
from seed_workspace import _validate_case_hierarchy, materialize_workspace


class FakeClient:
    files = {"src/a.txt": b"old\n", "src/b.txt": b"remove\n", "price.txt": b"700\n"}
    head_value = "head-1"
    proposal = "proposal-1"
    status_value = {"proposal": proposal, "status": "pending", "revision": None, "head": head_value}
    land_mode = "normal"

    def __init__(self, url, repository, token, **kwargs):
        self.url, self.repository, self.token = url, repository, token

    def head(self):
        return type(self).head_value

    def read(self, path, revision=None):
        return type(self).files.get(path)

    def range_snapshot(self, prefix="", kind="membership", revision=None):
        scoped = {
            path: object_id("blob", payload)
            for path, payload in type(self).files.items()
            if path.startswith(prefix)
        }
        return {"revision": revision or type(self).head_value, "prefix": prefix, "kind": kind, "paths": sorted(scoped), "files": scoped, "fingerprint": "fixed"}

    def read_blob(self, identifier):
        for payload in type(self).files.values():
            if object_id("blob", payload) == identifier:
                return payload
        raise SeedError("unknown fake blob")

    def propose(self, base, edits, **kwargs):
        type(self).last_proposal = {"base": base, "edits": dict(edits), **kwargs}
        return type(self).proposal

    def status(self, proposal):
        assert proposal == type(self).proposal
        return dict(type(self).status_value)

    def land(self, proposal):
        assert proposal == type(self).proposal
        if type(self).land_mode == "conflict":
            raise Conflict({"price.txt": {"expected": "old", "actual": "new"}})
        if type(self).land_mode == "lost-success":
            type(self).status_value = {"proposal": proposal, "status": "landed", "revision": "rev-2", "head": "rev-2"}
            raise SeedError("connection lost after server response")
        type(self).status_value = {"proposal": proposal, "status": "landed", "revision": "rev-2", "head": "rev-2"}
        return dict(type(self).status_value)


class AgentTests(unittest.TestCase):
    def setUp(self):
        temporary_parent = Path("artifacts/tmp").resolve()
        temporary_parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temporary_parent)
        self.root = Path(self.temp.name)
        self.state = self.root / "task.json"
        FakeClient.files = {"src/a.txt": b"old\n", "src/b.txt": b"remove\n", "price.txt": b"700\n"}
        FakeClient.head_value = "head-1"
        FakeClient.status_value = {"proposal": "proposal-1", "status": "pending", "revision": None, "head": "head-1"}
        FakeClient.land_mode = "normal"
        self.environment = patch.dict(os.environ, {"SEED_TOKEN": "test-token"})
        self.environment.start()
        self.client_patch = patch.object(seed_agent, "Client", FakeClient)
        self.client_patch.start()

    def tearDown(self):
        self.client_patch.stop()
        self.environment.stop()
        self.temp.cleanup()

    def invoke(self, *command):
        output = io.StringIO()
        arguments = ["--url", "http://127.0.0.1:7331", "--allow-http", "--repo", "demo", "--state", str(self.state), *command]
        with redirect_stdout(output):
            code = seed_agent.main(arguments)
        return code, json.loads(output.getvalue())

    def state_data(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def freeze(self):
        self.assertEqual(self.invoke("begin")[0], 0)
        self.assertEqual(self.invoke("write", "price.txt", "--content", "800\n")[0], 0)
        self.assertEqual(self.invoke("propose")[0], 0)

    def test_restart_restores_reads_and_overlay_then_freezes(self):
        self.assertEqual(self.invoke("begin")[0], 0)
        self.assertEqual(self.invoke("read", "price.txt")[1]["content"]["data"], "700\n")
        self.assertEqual(self.invoke("write", "price.txt", "--content", "800\n")[0], 0)
        code, diff = self.invoke("diff")
        self.assertEqual(code, 0)
        self.assertEqual(diff["changes"][0]["before"], "700\n")
        self.assertEqual(self.invoke("propose")[0], 0)
        state = self.state_data()
        self.assertEqual(state["reads"], ["price.txt"])
        self.assertEqual(base64.b64decode(state["edits"]["price.txt"]), b"800\n")

    def test_unknown_success_is_recovered_on_next_open(self):
        self.freeze()
        FakeClient.land_mode = "lost-success"
        self.assertEqual(self.invoke("land")[0], 2)
        self.assertIsNone(self.state_data()["result"])
        code, status = self.invoke("status")
        self.assertEqual(code, 0)
        self.assertEqual(status["status"], "landed")
        self.assertEqual(self.state_data()["result"]["revision"], "rev-2")

    def test_conflict_rework_creates_empty_current_task_without_old_inputs(self):
        self.freeze()
        FakeClient.land_mode = "conflict"
        self.assertEqual(self.invoke("land")[1]["status"], "conflict")
        FakeClient.head_value = "head-2"
        new_state = self.root / "rework.json"
        code, result = self.invoke("rework", "--new-state", str(new_state))
        self.assertEqual(code, 0)
        self.assertIn("price.txt", result["result"]["paths"])
        rework = json.loads(new_state.read_text(encoding="utf-8"))
        self.assertEqual(rework["base"], "head-2")
        self.assertEqual(rework["supersedes"], "proposal-1")
        self.assertEqual(rework["reads"], [])
        self.assertEqual(rework["edits"], {})

    def test_frozen_task_refuses_new_edits(self):
        self.freeze()
        code, failure = self.invoke("write", "other.txt", "--content", "no")
        self.assertEqual(code, 2)
        self.assertIn("frozen", failure["message"])
        self.assertNotIn("other.txt", self.state_data()["edits"])

    def test_frozen_task_refuses_new_observations(self):
        self.freeze()
        before = self.state_data()
        for command in (("read", "price.txt"), ("list",), ("search", "700")):
            code, failure = self.invoke(*command)
            self.assertEqual(code, 2)
            self.assertIn("frozen", failure["message"])
        self.assertEqual(self.state_data(), before)

    def test_task_lock_blocks_live_owner_and_crash_does_not_leave_a_stale_lock(self):
        lock_state = self.root / "locked-task.json"
        ready = self.root / "ready"
        script = (
            "from pathlib import Path; import time; from seed_agent import TaskFile; "
            f"guard=TaskFile({str(lock_state)!r}); context=guard.locked(); context.__enter__(); "
            f"Path({str(ready)!r}).write_text('ready'); time.sleep(60)"
        )
        owner = __import__("subprocess").Popen([sys.executable, "-c", script])
        try:
            for _ in range(750):
                if ready.exists():
                    break
                if owner.poll() is not None:
                    self.fail(f"lock owner exited before readiness: {owner.returncode}")
                time.sleep(0.02)
            self.assertTrue(ready.exists())
            with self.assertRaises(SeedError):
                with seed_agent.TaskFile(lock_state).locked():
                    pass
        finally:
            owner.terminate()
            owner.wait(timeout=5)
        # The lock file remains, but its advisory lock is automatically released
        # when the interrupted process exits.
        released = False
        for _ in range(50):
            try:
                with seed_agent.TaskFile(lock_state).locked():
                    released = True
                    break
            except SeedError:
                time.sleep(0.02)
        self.assertTrue(released)

    def test_repeated_workspace_run_rebuilds_scope_delta_from_pinned_base(self):
        self.assertEqual(self.invoke("begin")[0], 0)
        workspace = self.root / "repeat-scope"
        first = "from pathlib import Path; Path('a.txt').write_bytes(b'new\\n'); Path('new.txt').write_bytes(b'added\\n')"
        self.assertEqual(self.invoke("run", "--workspace", str(workspace), "--prefix", "src", "--", sys.executable, "-c", first)[0], 0)
        self.assertIn("src/a.txt", self.state_data()["edits"])
        second = "from pathlib import Path; Path('a.txt').write_bytes(b'old\\n'); Path('new.txt').unlink()"
        self.assertEqual(self.invoke("run", "--workspace", str(workspace), "--prefix", "src/", "--", sys.executable, "-c", second)[0], 0)
        edits = self.state_data()["edits"]
        self.assertNotIn("src/a.txt", edits)
        self.assertNotIn("src/new.txt", edits)
        code, failure = self.invoke("write", "src/a.txt", "--content", "different")
        self.assertEqual(code, 2)
        self.assertIn("materialized workspace", failure["message"])

    def test_manifest_left_by_interrupted_first_run_restores_subtree_guard_before_propose(self):
        self.assertEqual(self.invoke("begin")[0], 0)
        self.assertEqual(self.invoke("write", "src/a.txt", "--content", "candidate\n")[0], 0)
        state = self.state_data()
        session = seed_agent._session(FakeClient("http://127.0.0.1", "demo", "test"), state)
        workspace = self.root / "interrupted-workspace"
        materialize_workspace(session, workspace, prefix="src", state_path=self.state)
        # Simulate a crash before _run can persist Session.ranges/workspace.
        self.assertNotIn("workspace", self.state_data())
        FakeClient.files["src/a.txt"] = b"background\n"
        code, proposal = self.invoke("propose")
        self.assertEqual(code, 0, proposal)
        self.assertIn({"kind": "subtree", "prefix": "src/"}, FakeClient.last_proposal["ranges"])
        restored = self.state_data()
        self.assertEqual(restored["workspace"]["prefix"], "src")
        self.assertIn({"kind": "subtree", "prefix": "src/"}, restored["ranges"])

    def test_component_case_collisions_are_rejected_before_workspace_creation(self):
        for index, paths in enumerate((
            {"src/A/x.txt": b"x", "src/a/y.txt": b"y"},
            {"src/A": b"file", "src/a/y.txt": b"y"},
        )):
            self.state = self.root / f"case-{index}.json"
            FakeClient.files = paths
            self.assertEqual(self.invoke("begin")[0], 0)
            root = self.root / f"case-workspace-{index}"
            code, failure = self.invoke("run", "--workspace", str(root), "--prefix", "src", "--", sys.executable, "-c", "pass")
            self.assertEqual(code, 2)
            self.assertIn("Case-insensitive", failure["message"])
            self.assertFalse(root.exists())

        for paths in (("A/x.txt", "a/y.txt"), ("A", "a/y.txt")):
            with self.assertRaises(SeedError):
                _validate_case_hierarchy(list(paths))

    def test_run_terminates_descendants_that_inherit_command_pipes(self):
        for mode in ("parent-exit", "timeout"):
            self.state = self.root / f"tree-{mode}.json"
            FakeClient.files = {"src/a.txt": b"base\n"}
            self.assertEqual(self.invoke("begin")[0], 0)
            marker = self.root / f"{mode}-grandchild-alive"
            grandchild = (
                "import time; from pathlib import Path; "
                f"time.sleep(3); Path({str(marker)!r}).write_text('alive'); time.sleep(60)"
            )
            parent_tail = "" if mode == "parent-exit" else "; import time; time.sleep(60)"
            script = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {grandchild!r}]){parent_tail}"
            cleanup_spans = []

            def measured_cleanup(original):
                def measured(*args, **kwargs):
                    started = time.monotonic()
                    try:
                        return original(*args, **kwargs)
                    finally:
                        cleanup_spans.append(time.monotonic() - started)
                return measured

            # The deadline concerns process termination and pipe draining.
            # Process creation and durable task saves can take much longer on
            # a busy host, independently of whether a descendant was stopped.
            with patch.object(seed_agent, "_terminate_process_tree",
                              side_effect=measured_cleanup(seed_agent._terminate_process_tree)), \
                 patch.object(seed_agent, "_finish_reader",
                              side_effect=measured_cleanup(seed_agent._finish_reader)):
                code, result = self.invoke(
                    "run", "--workspace", str(self.root / f"tree-{mode}"), "--prefix", "src",
                    "--command-timeout", "2", "--", sys.executable, "-c", script,
                )
            self.assertEqual(code, 1 if mode == "timeout" else 0, result)
            self.assertEqual(result["timed_out"], mode == "timeout")
            time.sleep(3.2)
            self.assertFalse(marker.exists(), "a descendant survived the command containment")
            self.assertTrue(cleanup_spans)
            self.assertLess(sum(cleanup_spans), 10)

    def test_run_nonzero_exit_keeps_workspace_edits_but_fails_the_cli(self):
        self.assertEqual(self.invoke("begin")[0], 0)
        workspace = self.root / "failed-command"
        script = "from pathlib import Path; import sys; Path('a.txt').write_bytes(b'partial\\n'); sys.exit(7)"
        code, result = self.invoke(
            "run", "--workspace", str(workspace), "--prefix", "src", "--",
            sys.executable, "-c", script,
        )
        self.assertEqual(code, 1, result)
        self.assertEqual(result["returncode"], 7)
        self.assertFalse(result["timed_out"])
        self.assertEqual(base64.b64decode(self.state_data()["edits"]["src/a.txt"]), b"partial\n")

    def test_run_thread_start_failure_terminates_started_command(self):
        self.assertEqual(self.invoke("begin")[0], 0)
        state = self.state_data()
        client = FakeClient("http://127.0.0.1:7331", "demo", "test")
        session = seed_agent._session(client, state)
        arguments = seed_agent.parser().parse_args([
            "--url", "http://127.0.0.1:7331", "--allow-http", "--repo", "demo",
            "--state", str(self.state), "run", "--workspace", str(self.root / "thread-start"),
            "--prefix", "src", "--", sys.executable, "-c", "import time; time.sleep(60)",
        ])
        with seed_agent.TaskFile(self.state).locked() as task_file:
            with patch.object(seed_agent.threading.Thread, "start", side_effect=RuntimeError("thread start failed")):
                with patch.object(seed_agent, "_terminate_started_process", wraps=seed_agent._terminate_started_process) as cleanup:
                    with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                        seed_agent._run(arguments, state, session, task_file)
        cleanup.assert_called_once()

    def test_posix_run_observes_before_group_kill_and_reap(self):
        self.assertEqual(self.invoke("begin")[0], 0)
        state = self.state_data()
        session = seed_agent._session(FakeClient("http://127.0.0.1:7331", "demo", "test"), state)
        arguments = seed_agent.parser().parse_args([
            "--url", "http://127.0.0.1:7331", "--allow-http", "--repo", "demo",
            "--state", str(self.state), "run", "--workspace", str(self.root / "posix-order"),
            "--prefix", "src", "--", "ignored",
        ])
        order = []

        class Process:
            pid = 9876
            returncode = None
            stdout = io.BytesIO()
            stderr = io.BytesIO()

            def wait(self, timeout=None):
                order.append("reap")
                self.returncode = 0
                return 0

            def poll(self):
                return self.returncode

            def kill(self):
                order.append("direct-kill")

        process = Process()
        with seed_agent.TaskFile(self.state).locked() as task_file:
            with patch.object(seed_agent.subprocess, "Popen", return_value=process), \
                 patch.object(seed_agent, "_WindowsJob", return_value=object()), \
                 patch.object(seed_agent, "_resume_windows_process"), \
                 patch.object(seed_agent, "_posix_waitid_options", return_value=123), \
                 patch.object(seed_agent, "_wait_for_exit_without_reaping", side_effect=lambda *_: order.append("observe") or True), \
                 patch.object(seed_agent, "_terminate_process_tree", side_effect=lambda *_: order.append("group-kill")):
                seed_agent._run(arguments, state, session, task_file)
        self.assertEqual(order[:3], ["observe", "group-kill", "reap"])

    def test_workspace_roundtrip_and_reserved_name_safety(self):
        self.assertEqual(self.invoke("begin")[0], 0)
        workspace = self.root / "scope"
        script = "from pathlib import Path; Path('a.txt').write_bytes(b'new\\n'); Path('b.txt').unlink(); Path('new.txt').write_bytes(b'added\\n')"
        code, run = self.invoke("run", "--workspace", str(workspace), "--prefix", "src", "--", sys.executable, "-c", script)
        self.assertEqual(code, 0, run)
        state = self.state_data()
        self.assertEqual(base64.b64decode(state["edits"]["src/a.txt"]), b"new\n")
        self.assertIsNone(state["edits"]["src/b.txt"])
        self.assertEqual(base64.b64decode(state["edits"]["src/new.txt"]), b"added\n")
        self.assertIn({"kind": "subtree", "prefix": "src/"}, state["ranges"])

        unsafe = self.root / "unsafe.json"
        self.state = unsafe
        FakeClient.files = {"src/CON.txt": b"bad"}
        self.assertEqual(self.invoke("begin")[0], 0)
        code, failure = self.invoke("run", "--workspace", str(self.root / "unsafe-workspace"), "--prefix", "src", "--", sys.executable, "-c", "pass")
        self.assertEqual(code, 2)
        self.assertIn("Reserved Windows", failure["message"])
        self.assertFalse((self.root / "unsafe-workspace").exists())

        self.state = self.root / "linked.json"
        FakeClient.files = {"src/a.txt": b"safe\n"}
        self.assertEqual(self.invoke("begin")[0], 0)
        linked_workspace = self.root / "linked-workspace"
        self.assertEqual(self.invoke("run", "--workspace", str(linked_workspace), "--prefix", "src", "--", sys.executable, "-c", "pass")[0], 0)
        outside = self.root / "outside"
        outside.mkdir()
        try:
            os.symlink(outside, linked_workspace / "escape", target_is_directory=True)
        except OSError:
            # Some Windows policies deny unprivileged symlink creation.  The
            # reserved-name assertion above still exercises path rejection.
            return
        code, failure = self.invoke("run", "--workspace", str(linked_workspace), "--prefix", "src/", "--", sys.executable, "-c", "pass")
        self.assertEqual(code, 2)
        self.assertIn("Symlink or reparse", failure["message"])


if __name__ == "__main__":
    unittest.main()
