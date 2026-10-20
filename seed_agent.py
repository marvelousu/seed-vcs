"""Persistent JSON command adapter for an agent using Seed.

The state file intentionally contains task metadata and edits, never the bearer
token.  Each command obtains an exclusive lock, validates the task binding, and
atomically replaces the state file, so a crash cannot turn a proposal submission
into an ambiguous mutable task.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import signal
import time
from typing import Any, Iterator
from urllib.parse import urlsplit
from uuid import uuid4

from seed import Conflict, SeedError, file_path
from seed_client import Client, Session
from seed_workspace import collect_workspace_edits, materialize_workspace, workspace_manifest


TASK_FORMAT = "seed-agent/task/1"
MAX_COMMAND_OUTPUT = 64 * 1024


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _bytes_json(payload: bytes | None, *, truncated: bool = False) -> dict[str, Any] | None:
    if payload is None:
        return None
    truncated = truncated or len(payload) > MAX_COMMAND_OUTPUT
    payload = payload[:MAX_COMMAND_OUTPUT]
    try:
        result: dict[str, Any] = {"encoding": "utf-8", "data": payload.decode("utf-8")}
    except UnicodeDecodeError:
        result = {"encoding": "base64", "data": base64.b64encode(payload).decode("ascii")}
    if truncated:
        result["truncated"] = True
    return result


def _decode_edits(raw: Any) -> dict[str, bytes | None]:
    if not isinstance(raw, dict):
        raise SeedError("Task edits must be an object")
    result: dict[str, bytes | None] = {}
    for path, encoded in raw.items():
        path = file_path(path)
        if encoded is None:
            result[path] = None
        elif isinstance(encoded, str):
            try:
                result[path] = base64.b64decode(encoded.encode("ascii"), validate=True)
            except (UnicodeEncodeError, ValueError) as exc:
                raise SeedError(f"Invalid base64 task edit for {path!r}") from exc
        else:
            raise SeedError("Task edit values must be base64 strings or null")
    return result


def _encode_edits(edits: dict[str, bytes | None]) -> dict[str, str | None]:
    return {
        path: None if payload is None else base64.b64encode(payload).decode("ascii")
        for path, payload in sorted(edits.items())
    }


def _normal_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SeedError("Task server URL must not include credentials, query or fragment")
    return value.rstrip("/")


def _ranges(raw: Any) -> set[tuple[str, str]]:
    if not isinstance(raw, list):
        raise SeedError("Task ranges must be a list")
    result: set[tuple[str, str]] = set()
    for descriptor in raw:
        if not isinstance(descriptor, dict) or set(descriptor) != {"kind", "prefix"}:
            raise SeedError("Invalid task range descriptor")
        kind, prefix = descriptor["kind"], descriptor["prefix"]
        if kind not in {"membership", "subtree"} or not isinstance(prefix, str):
            raise SeedError("Invalid task range descriptor")
        if prefix:
            # Range prefixes deliberately have a trailing slash, unlike file
            # paths.  Validate the path portion before restoring that marker.
            prefix = file_path(prefix.rstrip("/")) + "/"
        result.add((kind, prefix))
    return result


def _encoded_ranges(ranges: set[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"kind": kind, "prefix": prefix} for kind, prefix in sorted(ranges)]


def _overlay_digest(edits: dict[str, bytes | None]) -> str:
    digest = hashlib.sha256()
    for path, payload in sorted(edits.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(b"\xff" if payload is None else hashlib.sha256(payload).digest())
    return digest.hexdigest()


class TaskFile:
    """An exclusive, atomic task-state file with an OS-released advisory lock."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().absolute()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._locked = False
        self._lock_handle = None

    @contextmanager
    def locked(self) -> Iterator["TaskFile"]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+b")
        try:
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            self._lock_byte(handle)
        except BaseException:
            handle.close()
            raise
        self._lock_handle = handle
        self._locked = True
        try:
            yield self
        finally:
            self._locked = False
            self._unlock_byte(handle)
            handle.close()
            self._lock_handle = None

    @staticmethod
    def _lock_byte(handle: Any) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - exercised by Unix users
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SeedError(f"Task state is already in use: {handle.name}") from exc

    @staticmethod
    def _unlock_byte(handle: Any) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - exercised by Unix users
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise SeedError("Task state does not exist; run begin first") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise SeedError("Task state is not valid JSON") from exc
        _validate_state(value)
        return value

    def create(self, value: dict[str, Any]) -> None:
        if self.path.exists():
            raise SeedError("Task state already exists; choose a new --state path")
        _validate_state(value)
        self.save(value, creating=True)

    def save(self, value: dict[str, Any], *, creating: bool = False) -> None:
        if not self._locked:
            raise RuntimeError("Task state writes require the task lock")
        _validate_state(value)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            with open(temporary, "xb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            if creating and self.path.exists():
                raise SeedError("Task state already exists")
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _validate_state(value: Any) -> None:
    if not isinstance(value, dict) or value.get("format") != TASK_FORMAT:
        raise SeedError("Unsupported task state format")
    required = {"format", "task", "url", "repository", "base", "reads", "ranges", "edits", "proposal", "result", "supersedes", "retries", "metrics"}
    if set(value) - {"workspace", *required} or not required <= set(value):
        raise SeedError("Task state has an unsupported schema")
    if not all(isinstance(value[key], str) and value[key] for key in ("task", "url", "repository", "base")):
        raise SeedError("Task state has invalid task binding")
    _normal_url(value["url"])
    if not isinstance(value["reads"], list) or len(set(value["reads"])) != len(value["reads"]):
        raise SeedError("Task reads must be a unique list")
    for path in value["reads"]:
        file_path(path)
    _ranges(value["ranges"])
    _decode_edits(value["edits"])
    if value["proposal"] is not None and not isinstance(value["proposal"], str):
        raise SeedError("Task proposal must be a string or null")
    if value["supersedes"] is not None and not isinstance(value["supersedes"], str):
        raise SeedError("Task supersedes must be a string or null")
    if value["result"] is not None and not isinstance(value["result"], dict):
        raise SeedError("Task result must be an object or null")
    if not isinstance(value["retries"], int) or value["retries"] < 0 or not isinstance(value["metrics"], list):
        raise SeedError("Task counters are invalid")


def _new_state(client: Client, url: str, repository: str, *, supersedes: str | None = None) -> dict[str, Any]:
    return {
        "format": TASK_FORMAT,
        "task": str(uuid4()),
        "url": _normal_url(url),
        "repository": repository,
        "base": client.head(),
        "reads": [],
        "ranges": [],
        "edits": {},
        "proposal": None,
        "result": None,
        "supersedes": supersedes,
        "retries": 0,
        "metrics": [],
    }


def _session(client: Client, state: dict[str, Any]) -> Session:
    session = Session(client, state["base"])
    session.reads = set(state["reads"])
    session.ranges = _ranges(state["ranges"])
    session.edits = _decode_edits(state["edits"])
    return session


def _save_session(state: dict[str, Any], session: Session) -> None:
    state["reads"] = sorted(session.reads)
    state["ranges"] = _encoded_ranges(session.ranges)
    state["edits"] = _encode_edits(session.edits)


def _client(arguments: argparse.Namespace) -> Client:
    token = os.environ.get("SEED_TOKEN")
    if not token:
        raise SeedError("SEED_TOKEN must be set in the environment")
    return Client(
        arguments.url,
        arguments.repo,
        token,
        timeout=arguments.timeout,
        allow_http=arguments.allow_http,
    )


def _bound_state(file: TaskFile, arguments: argparse.Namespace, client: Client) -> dict[str, Any]:
    state = file.load()
    if state["url"] != _normal_url(arguments.url) or state["repository"] != arguments.repo:
        raise SeedError("Task state is bound to a different --url or --repo")
    return state


def _record(state: dict[str, Any], event: str, **fields: Any) -> None:
    state["metrics"].append({"event": event, "at": int(time.time()), **fields})


def _record_transport(state: dict[str, Any], client: Client, action: str) -> bool:
    """Persist the HTTP work spent by this single CLI invocation.

    The CLI creates a client per invocation, so retaining these aggregate
    counters on the task is what makes task-level transport traces possible.
    Test doubles and alternate clients may omit this optional surface.
    """
    metrics = getattr(client, "metrics", None)
    if not isinstance(metrics, dict):
        return False
    keys = ("requests", "upload_bytes", "download_bytes", "request_seconds")
    if not all(isinstance(metrics.get(key), (int, float)) for key in keys):
        return False
    _record(
        state,
        "transport",
        action=action,
        requests=int(metrics["requests"]),
        upload_bytes=int(metrics["upload_bytes"]),
        download_bytes=int(metrics["download_bytes"]),
        request_seconds=round(float(metrics["request_seconds"]), 6),
    )
    return True


def _recover_unknown(state: dict[str, Any], client: Client) -> bool:
    """Resolve a response lost after land without mutating the proposal itself."""
    proposal = state["proposal"]
    if proposal is None or state["result"] is not None:
        return False
    result = client.status(proposal)
    if result.get("status") == "landed":
        state["result"] = result
        _record(state, "recovered-land", revision=result.get("revision"))
        return True
    return False


def _restore_workspace_guard(state: dict[str, Any], session: Session, state_file: TaskFile) -> bool:
    """Recover a guard if a crash left a workspace manifest before task save."""
    manifest = workspace_manifest(state_file.path)
    if manifest is None:
        return False
    if manifest.get("base") != session.base or not isinstance(manifest.get("prefix"), str):
        raise SeedError("Workspace manifest does not match this task's base revision")
    before_ranges = set(session.ranges)
    session.track_scope(manifest["prefix"], "subtree")
    workspace = {
        "workspace": manifest["root"],
        "prefix": manifest["prefix"],
        "files": len(manifest.get("base_files", {})),
        "manifest": str(state_file.path.with_name(state_file.path.name + ".workspace.json")),
    }
    changed = state.get("workspace") != workspace
    state["workspace"] = workspace
    _save_session(state, session)
    return changed or session.ranges != before_ranges


def _assert_mutable(state: dict[str, Any]) -> None:
    if state["proposal"] is not None:
        raise SeedError("Task is frozen by a proposal; use rework to create a new task")


def _assert_no_workspace_mixing(state: dict[str, Any]) -> None:
    if state.get("workspace") is not None:
        raise SeedError("Use the materialized workspace for edits after run has begun")


def _content(arguments: argparse.Namespace) -> bytes:
    choices = (
        int(arguments.content is not None)
        + int(arguments.file is not None)
        + int(bool(getattr(arguments, "stdin", False)))
    )
    if choices != 1:
        raise SeedError("write requires exactly one of --content, --file, or --stdin")
    if arguments.content is not None:
        return arguments.content.encode("utf-8")
    if arguments.file is not None:
        return Path(arguments.file).read_bytes()
    return sys.stdin.buffer.read()


def _safe_command(command: list[str]) -> list[str]:
    result = []
    redact_next = False
    for part in command:
        if redact_next:
            result.append("<redacted-argument>")
            redact_next = False
            continue
        lowered = part.casefold()
        sensitive = ("token", "secret", "password", "api-key", "apikey")
        if any(name + "=" in lowered for name in sensitive):
            result.append("<redacted-argument>")
        elif lowered.lstrip("-") in sensitive:
            result.append(part)
            redact_next = True
        else:
            result.append(part)
    return result


def _scope_prefix(prefix: str) -> str:
    return "" if not prefix else file_path(prefix.rstrip("/"))


def _drain_limited(stream: Any, destination: dict[str, Any]) -> None:
    """Drain a child pipe without retaining an unbounded command transcript."""
    chunks: list[bytes] = []
    size = 0
    while chunk := stream.read(8192):
        remaining = MAX_COMMAND_OUTPUT - size
        if remaining > 0:
            chunks.append(chunk[:remaining])
            size += min(len(chunk), remaining)
        if len(chunk) > remaining:
            destination["truncated"] = True
    destination["payload"] = b"".join(chunks)


class _WindowsJob:
    """Contain a command tree so an exited parent cannot leave pipe holders."""

    def __init__(self, process: subprocess.Popen[bytes]):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("per_process_user_time", ctypes.c_longlong),
                ("per_job_user_time", ctypes.c_longlong),
                ("limit_flags", wintypes.DWORD),
                ("minimum_working_set", ctypes.c_size_t),
                ("maximum_working_set", ctypes.c_size_t),
                ("active_process_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority_class", wintypes.DWORD),
                ("scheduling_class", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "read_operation_count", "write_operation_count", "other_operation_count",
                "read_transfer_count", "write_transfer_count", "other_transfer_count",
            )]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimit), ("io", IoCounters),
                ("process_memory_limit", ctypes.c_size_t), ("job_memory_limit", ctypes.c_size_t),
                ("peak_process_memory_used", ctypes.c_size_t), ("peak_job_memory_used", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise SeedError("Could not create a Windows command job")
        information = ExtendedLimit()
        information.basic.limit_flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(information), ctypes.sizeof(information)):
            kernel32.CloseHandle(job)
            raise SeedError("Could not configure a Windows command job")
        if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
            kernel32.CloseHandle(job)
            raise SeedError("Could not contain the Windows command process tree")
        self.handle = job
        self._kernel32 = kernel32
        self._ctypes = ctypes

    def close(self) -> None:
        if self.handle is not None:
            # Close alone should honor KILL_ON_JOB_CLOSE, but explicitly ending
            # the job keeps containment correct on hosts that already place the
            # runner in a parent job with different inherited limits.
            if not self._kernel32.TerminateJobObject(self.handle, 1):
                error = self._ctypes.get_last_error()
                self._kernel32.CloseHandle(self.handle)
                self.handle = None
                raise SeedError(f"Could not terminate the Windows command job ({error})")
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


def _posix_waitid_options() -> int | None:
    """Return the non-reaping wait options required for safe group cleanup."""
    if os.name == "nt":
        return None
    waitid = getattr(os, "waitid", None)
    required = ("P_PID", "WEXITED", "WNOWAIT", "WNOHANG")
    if not callable(waitid) or not all(hasattr(os, name) for name in required):
        raise SeedError("POSIX run requires os.waitid with WNOWAIT for safe process-group cleanup")
    return os.WEXITED | os.WNOWAIT | os.WNOHANG


def _wait_for_exit_without_reaping(process: subprocess.Popen[bytes], timeout: float, options: int) -> bool:
    """Wait for the leader's exit while retaining its PID/PGID until killpg."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            observed = os.waitid(os.P_PID, process.pid, options)
        except InterruptedError:
            continue
        except ChildProcessError as exc:
            raise SeedError("Command leader was reaped before safe process-group cleanup") from exc
        if observed is not None:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.02, remaining))


def _terminate_process_tree(process: subprocess.Popen[bytes], job: _WindowsJob) -> None:
    if os.name == "nt":
        # The direct process was assigned while suspended.  Windows places any
        # descendants it creates in this job, so no PID-snapshot fallback can
        # accidentally target a process after PID reuse.
        job.close()
        return
    try:  # pragma: no cover - Windows is the primary supported runner host
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _resume_windows_process(pid: int) -> None:
    """Resume the one initial thread created with CREATE_SUSPENDED."""
    if os.name != "nt":  # pragma: no cover - Windows runner only
        return
    import ctypes
    from ctypes import wintypes

    class ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise SeedError("Could not inspect a suspended Windows command process")
    entry = ThreadEntry()
    entry.dwSize = ctypes.sizeof(entry)
    thread_id = None
    try:
        available = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while available:
            if entry.th32OwnerProcessID == pid:
                thread_id = entry.th32ThreadID
                break
            available = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if thread_id is None:
        raise SeedError("Suspended Windows command thread disappeared before resume")
    handle = kernel32.OpenThread(0x0002, False, thread_id)  # THREAD_SUSPEND_RESUME
    if not handle:
        raise SeedError("Could not resume a suspended Windows command process")
    try:
        if kernel32.ResumeThread(handle) == 0xFFFFFFFF:
            raise SeedError("Could not resume a suspended Windows command process")
    finally:
        kernel32.CloseHandle(handle)


def _terminate_started_process(process: subprocess.Popen[bytes], job: _WindowsJob | None) -> None:
    """End a command tree, including partially initialized Windows commands."""
    if job is not None:
        _terminate_process_tree(process, job)
    elif process.poll() is None:
        process.kill()


def _finish_reader(stream: Any, reader: threading.Thread | None, destination: dict[str, Any]) -> None:
    """Bound output draining without closing a pipe owned by a live reader."""
    # Thread.start itself may fail.  Joining that Thread would replace the
    # original setup exception with "cannot join thread before it is started".
    if reader is not None and reader.ident is not None:
        reader.join(timeout=1)
        if reader.is_alive():
            destination["truncated"] = True
            return
    if stream is not None and not stream.closed:
        stream.close()


def _run(arguments: argparse.Namespace, state: dict[str, Any], session: Session, state_file: TaskFile) -> dict[str, Any]:
    _assert_mutable(state)
    if not 1 <= arguments.command_timeout <= 3600:
        raise SeedError("run --command-timeout must be between 1 and 3600 seconds")
    command = list(arguments.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise SeedError("run requires a command after --")
    prefix = _scope_prefix(arguments.prefix)
    manifest = workspace_manifest(state_file.path)
    requested_root = str(Path(arguments.workspace).expanduser().absolute())
    if manifest is None:
        workspace = materialize_workspace(session, requested_root, prefix=prefix, state_path=state_file.path)
    else:
        if manifest["root"] != requested_root or manifest["prefix"] != prefix or manifest["base"] != session.base:
            raise SeedError("Workspace does not match this task, prefix, or base revision")
        workspace = {"workspace": requested_root, "prefix": prefix, "files": len(manifest["base_files"]), "manifest": str(state_file.path.with_name(state_file.path.name + ".workspace.json"))}

    # Save the workspace binding and subtree guard before launching user code.
    # If the process dies after materialization, the manifest also lets the next
    # command restore this state before it can propose or land anything.
    session.track_scope(prefix, "subtree")
    state["workspace"] = workspace
    _save_session(state, session)
    state_file.save(state)

    started = time.monotonic()
    child_environment = os.environ.copy()
    child_environment.pop("SEED_TOKEN", None)
    # A POSIX leader must not be reaped before killpg: otherwise its PID can be
    # recycled into an unrelated process group.  Refuse to launch where Python
    # cannot provide the non-reaping wait primitive.
    posix_wait_options = _posix_waitid_options()
    process: subprocess.Popen[bytes] | None = None
    job: _WindowsJob | None = None
    stdout_thread: threading.Thread | None = None
    stderr_thread: threading.Thread | None = None
    stdout: dict[str, Any] = {"payload": b"", "truncated": False}
    stderr: dict[str, Any] = {"payload": b"", "truncated": False}
    contained = False
    try:
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004  # CREATE_SUSPENDED
            if os.name == "nt" else 0
        )
        process = subprocess.Popen(
            command,
            cwd=requested_root,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_environment,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
        job = _WindowsJob(process)
        _resume_windows_process(process.pid)
        stdout_thread = threading.Thread(target=_drain_limited, args=(process.stdout, stdout), daemon=True)
        stderr_thread = threading.Thread(target=_drain_limited, args=(process.stderr, stderr), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        if posix_wait_options is None:
            try:
                process.wait(timeout=arguments.command_timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                timed_out = True
        else:
            timed_out = not _wait_for_exit_without_reaping(process, arguments.command_timeout, posix_wait_options)
        _terminate_process_tree(process, job)
        contained = True
        if timed_out:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:  # pragma: no cover - OS containment failure
                raise SeedError("Timed out waiting for terminated command process") from exc
        elif posix_wait_options is not None:
            # The non-reaping wait above observed the exited leader.  Reap it
            # only after killpg has made the original group identity unusable.
            process.wait(timeout=5)
        _finish_reader(process.stdout, stdout_thread, stdout)
        _finish_reader(process.stderr, stderr_thread, stderr)
        process_result = {
            "returncode": None if timed_out else process.returncode,
            "stdout": _bytes_json(stdout["payload"], truncated=stdout["truncated"]),
            "stderr": _bytes_json(stderr["payload"], truncated=stderr["truncated"]),
            "timed_out": timed_out,
        }
    finally:
        # This includes exceptions from thread start, waits, and interrupt
        # delivery.  Preserve that original exception while ensuring a
        # persistent in-process caller cannot retain a command tree or handle.
        if process is not None:
            if not contained:
                try:
                    _terminate_started_process(process, job)
                except BaseException:
                    pass
            try:
                if process.poll() is None:
                    process.wait(timeout=5)
            except BaseException:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except BaseException:
                    pass
            _finish_reader(process.stdout, stdout_thread, stdout)
            _finish_reader(process.stderr, stderr_thread, stderr)

    # Replace (rather than add to) the complete scope delta.  This lets a later
    # command revert a prior edit in the workspace without retaining a stale
    # overlay entry in the persistent task.
    def in_scope(path: str) -> bool:
        return not prefix or path.startswith(prefix + "/")

    for path in list(session.edits):
        if in_scope(path):
            del session.edits[path]
    for path, payload in collect_workspace_edits(requested_root, state_path=state_file.path).items():
        session.write(path, payload)
    _save_session(state, session)
    _record(
        state,
        "run",
        command=_safe_command(command),
        returncode=process_result["returncode"],
        timed_out=process_result["timed_out"],
        elapsed_ms=round((time.monotonic() - started) * 1000),
        base=session.base,
        overlay=_overlay_digest(session.edits),
        dependency_mode="declared-scope",
    )
    return {"workspace": workspace, "dependency_mode": "declared-scope", "capture": "not-sandboxed", "overlay": _overlay_digest(session.edits), **process_result}


def execute(arguments: argparse.Namespace) -> dict[str, Any]:
    state_file = TaskFile(arguments.state)
    client = _client(arguments)
    with state_file.locked():
        if arguments.action == "begin":
            state = _new_state(client, arguments.url, arguments.repo)
            _record_transport(state, client, "begin")
            state_file.create(state)
            return {"task": state["task"], "base": state["base"], "state": str(state_file.path)}

        state = _bound_state(state_file, arguments, client)
        changed = _recover_unknown(state, client)
        session = _session(client, state)
        workspace_restored = _restore_workspace_guard(state, session, state_file)
        if workspace_restored:
            # Persist recovery before processing any action.  In particular, a
            # later proposal cannot bypass a scope guard after a first-run crash.
            state_file.save(state)
        changed = changed or workspace_restored

        if arguments.action == "read":
            _assert_mutable(state)
            payload = session.read_base(arguments.path) if arguments.base else session.read(arguments.path)
            _save_session(state, session)
            changed = True
            result = {"path": file_path(arguments.path), "base": bool(arguments.base), "content": _bytes_json(payload)}
        elif arguments.action == "list":
            _assert_mutable(state)
            result = {"prefix": arguments.prefix, "files": session.files(prefix=arguments.prefix)}
            _save_session(state, session)
            changed = True
        elif arguments.action == "search":
            _assert_mutable(state)
            result = session.search(arguments.query, prefix=arguments.prefix, limit=arguments.limit)
            _save_session(state, session)
            changed = True
        elif arguments.action == "write":
            _assert_mutable(state)
            _assert_no_workspace_mixing(state)
            payload = _content(arguments)
            session.write(arguments.path, payload)
            _save_session(state, session)
            changed = True
            result = {"path": file_path(arguments.path), "bytes": len(payload), "overlay": _overlay_digest(session.edits)}
        elif arguments.action == "delete":
            _assert_mutable(state)
            _assert_no_workspace_mixing(state)
            session.write(arguments.path, None)
            _save_session(state, session)
            changed = True
            result = {"path": file_path(arguments.path), "deleted": True, "overlay": _overlay_digest(session.edits)}
        elif arguments.action == "diff":
            result = {"changes": session.diff()}
        elif arguments.action == "propose":
            _assert_mutable(state)
            proposal = session.propose(author=arguments.author, message=arguments.message, supersedes=state["supersedes"])
            # This is deliberately saved before any land attempt.
            state["proposal"] = proposal
            _save_session(state, session)
            _record(state, "proposed", proposal=proposal, overlay=_overlay_digest(session.edits))
            changed = True
            result = {"proposal": proposal, "status": "pending", "base": session.base}
        elif arguments.action == "status":
            if state["proposal"] is None:
                result = {"proposal": None, "status": "mutable", "base": session.base}
            else:
                result = client.status(state["proposal"])
                if result.get("status") == "landed":
                    state["result"] = result
                    changed = True
        elif arguments.action == "describe":
            if state["proposal"] is None:
                raise SeedError("describe requires a frozen proposal")
            result = client.describe(state["proposal"])
        elif arguments.action == "land":
            if state["proposal"] is None:
                raise SeedError("propose before land")
            if state["result"] is not None and state["result"].get("status") == "landed":
                result = state["result"]
            else:
                state["retries"] += 1
                _record(state, "land-attempt", proposal=state["proposal"], attempt=state["retries"])
                state_file.save(state)
                try:
                    result = client.land(state["proposal"])
                except Conflict as exc:
                    result = {"status": "conflict", "paths": exc.differences, "head": exc.head}
                if result.get("status") in {"landed", "already-landed", "conflict"}:
                    state["result"] = result
                changed = True
        elif arguments.action == "rework":
            if state["proposal"] is None:
                raise SeedError("rework requires a frozen proposal")
            new_path = Path(arguments.new_state).expanduser().absolute()
            if new_path == state_file.path:
                raise SeedError("rework --new-state must differ from the original task state")
            replacement = TaskFile(new_path)
            with replacement.locked():
                new_state = _new_state(client, arguments.url, arguments.repo, supersedes=state["proposal"])
                replacement.create(new_state)
            result = {"previous_task": state["task"], "proposal": state["proposal"], "result": state["result"], "diff": session.diff(), "new_task": new_state["task"], "new_state": str(new_path), "base": new_state["base"], "supersedes": state["proposal"]}
        elif arguments.action == "run":
            result = _run(arguments, state, session, state_file)
            changed = True
        else:  # pragma: no cover - argparse keeps this unreachable
            raise SeedError("Unknown command")

        if arguments.action in {"read", "list", "search"}:
            # Tie an observation to its base and overlay without duplicating
            # source contents or storing a potentially sensitive search query.
            _record(state, "observation", operation=arguments.action,
                    base=session.base, overlay=_overlay_digest(session.edits),
                    response_digest=hashlib.sha256(
                        json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    ).hexdigest())
            changed = True
        # A client lives for one CLI command; preserve its aggregate transport
        # counters with this task after a successful action.
        changed = _record_transport(state, client, arguments.action) or changed
        if changed:
            state_file.save(state)
        return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="seed-agent")
    root.add_argument("--url", required=True)
    root.add_argument("--repo", required=True)
    root.add_argument("--state", required=True)
    root.add_argument("--timeout", type=float, default=30)
    root.add_argument("--allow-http", action="store_true")
    commands = root.add_subparsers(dest="action", required=True)
    commands.add_parser("begin")
    read = commands.add_parser("read")
    read.add_argument("path")
    read.add_argument("--base", action="store_true")
    listing = commands.add_parser("list")
    listing.add_argument("--prefix", default="")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--prefix", default="")
    search.add_argument("--limit", type=int, default=100)
    write = commands.add_parser("write")
    write.add_argument("path")
    write.add_argument("--content")
    write.add_argument("--file")
    write.add_argument("--stdin", action="store_true")
    delete = commands.add_parser("delete")
    delete.add_argument("path")
    commands.add_parser("diff")
    propose = commands.add_parser("propose")
    propose.add_argument("--author", default="")
    propose.add_argument("--message", default="")
    commands.add_parser("status")
    commands.add_parser("describe")
    commands.add_parser("land")
    rework = commands.add_parser("rework")
    rework.add_argument("--new-state", required=True)
    run = commands.add_parser("run")
    run.add_argument("--workspace", required=True)
    run.add_argument("--prefix", default="")
    run.add_argument("--command-timeout", type=float, default=60)
    run.add_argument("command", nargs=argparse.REMAINDER)
    return root


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parser().parse_args(argv)
        result = execute(arguments)
        _json(result)
        # Preserve a completed run's workspace delta for diagnosis, while
        # giving shell callers the normal failure signal for a failed command.
        if arguments.action == "run" and (
            result.get("timed_out") or result.get("returncode") not in {0, None}
        ):
            return 1
        return 0
    except (SeedError, ValueError, OSError, subprocess.SubprocessError) as exc:
        _json({"status": "error", "message": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
