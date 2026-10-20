"""Revision-bound validation attestations and optional strict publication.

Commands run in a separate trusted local worker, never inside the HTTP server.
Repository write permission cannot attest tests or promote a deployment target.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from seed import Conflict, SeedError, canonical, object_id


ID = re.compile(r"[0-9a-f]{64}\Z")


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise SeedError("Expected a SHA-256 identifier")
    return value


def _get(db, key):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else json.loads(row[0])


def _set(db, key, value):
    encoded = canonical(value).decode("utf-8")
    db.execute("INSERT OR IGNORE INTO meta VALUES (?, ?)", (key, encoded))
    db.execute("UPDATE meta SET value=? WHERE key=?", (encoded, key))


def _immutable(db, key, value):
    encoded = canonical(value).decode("utf-8")
    db.execute("INSERT OR IGNORE INTO meta VALUES (?, ?)", (key, encoded))
    if _get(db, key) != value:
        raise SeedError("Immutable workflow record changed")


def _manifest(store, db, revision):
    seq = store._revision(db, revision)["seq"]
    return store._range_files(db, "", seq)


def tree_id(files):
    return object_id("tree", canonical(files))


def create_candidate(store, proposals):
    proposals = list(proposals)
    if not 1 <= len(proposals) <= 256 or len(set(proposals)) != len(proposals):
        raise SeedError("A candidate requires 1 to 256 distinct proposals")
    for proposal in proposals:
        identifier(proposal)
    with store._db(write=True) as db:
        store._lock_head(db)
        head = store._head(db)
        db.execute("SAVEPOINT candidate_preview")
        try:
            for proposal in proposals:
                if store._land(db, proposal)["status"] != "landed":
                    raise SeedError("Candidate proposals must not already be landed")
            revision = store._head(db)
            files = _manifest(store, db, revision)
        finally:
            db.execute("ROLLBACK TO SAVEPOINT candidate_preview")
            db.execute("RELEASE SAVEPOINT candidate_preview")
        body = {"format": "seed/candidate/1", "head": head,
                "proposals": proposals, "revision": revision,
                "files": files, "tree": tree_id(files)}
        candidate = object_id("candidate", canonical(body))
        _immutable(db, "candidate:" + candidate, body)
        return {"candidate": candidate, **body}


def _candidate(db, candidate):
    identifier(candidate)
    body = _get(db, "candidate:" + candidate)
    if body is None or object_id("candidate", canonical(body)) != candidate:
        raise SeedError("Unknown or damaged candidate")
    return body


def candidate(store, value):
    with store._db() as db:
        return {"candidate": value, **_candidate(db, value)}


def set_policy(store, configurations):
    configurations = sorted(set(configurations))
    if not 1 <= len(configurations) <= 16:
        raise SeedError("Validation policy requires 1 to 16 configurations")
    for value in configurations:
        identifier(value)
    body = {"configurations": configurations}
    policy = {"id": object_id("validation-policy", canonical(body)), **body}
    with store._db(write=True) as db:
        store._lock_head(db)
        _set(db, "validation-policy", policy)
    return policy


def _checks(db, revision, tree, policy):
    if policy is None:
        return None
    results = []
    for configuration in policy["configurations"]:
        result_id = _get(db, f"validation-latest:{revision}:{configuration}")
        result = _get(db, "validation:" + result_id) if result_id else None
        if result is None:
            return None
        report = result["report"]
        if (object_id("validation", canonical(report)) != result_id
                or report["revision"] != revision or report["tree"] != tree
                or report["configuration"] != configuration or not report["passed"]):
            return None
        results.append(result_id)
    return results


def _pointer(store, db, revision, policy):
    files = _manifest(store, db, revision)
    checks = _checks(db, revision, tree_id(files), policy)
    if checks is None:
        return None
    return {"revision": revision, "tree": tree_id(files),
            "policy": policy["id"], "validations": checks}


def _advance_verified(store, db, revision):
    policy = _get(db, "validation-policy")
    pointer = _pointer(store, db, revision, policy)
    if pointer is None:
        return
    old = _get(db, "verified")
    if (old is None or old["policy"] != policy["id"]
            or store._revision(db, old["revision"])["seq"] <= store._revision(db, revision)["seq"]):
        _set(db, "verified", pointer)


def record_validation(store, *, kind, subject, configuration, environment,
                      passed, returncode, timed_out=False, elapsed_ms=0,
                      log_digest, request):
    if kind not in {"candidate", "revision"}:
        raise SeedError("Validation subject must be candidate or revision")
    for value in (subject, configuration, environment, log_digest):
        identifier(value)
    if (type(passed) is not bool or type(timed_out) is not bool
            or (returncode is not None and type(returncode) is not int)
            or type(elapsed_ms) is not int or not 0 <= elapsed_ms <= 86_400_000
            or not isinstance(request, str) or not 1 <= len(request) <= 128):
        raise SeedError("Invalid validation report")
    if passed and (returncode != 0 or timed_out):
        raise SeedError("A failed or timed-out command cannot pass validation")
    with store._db(write=True) as db:
        store._lock_head(db)
        if kind == "candidate":
            body = _candidate(db, subject)
            revision, tree = body["revision"], body["tree"]
        else:
            revision, tree = subject, tree_id(_manifest(store, db, subject))
        report = dict(kind=kind, subject=subject, revision=revision, tree=tree,
                      configuration=configuration, environment=environment,
                      passed=passed, returncode=returncode, timed_out=timed_out,
                      elapsed_ms=elapsed_ms, log_digest=log_digest, request=request)
        result_id = object_id("validation", canonical(report))
        previous = _get(db, "validation:" + result_id)
        if previous is not None:
            return previous  # An HTTP retry must not resurrect an older result.
        sequence = (_get(db, "validation-counter") or 0) + 1
        result = {"validation": result_id, "sequence": sequence,
                  "recorded_at": int(time.time()), "report": report}
        _immutable(db, "validation:" + result_id, result)
        _set(db, "validation-counter", sequence)
        _set(db, f"validation-latest:{revision}:{configuration}", result_id)
        if db.execute("SELECT id FROM revisions WHERE id=?", (revision,)).fetchone():
            _advance_verified(store, db, revision)
        return result


def validation_state(store):
    with store._db() as db:
        policy = _get(db, "validation-policy")
        result = {"head": store._head(db), "policy": policy,
                  "verified": None, "deployable": None}
        for name in ("verified", "deployable"):
            pointer = _get(db, name)
            if pointer and policy and pointer["policy"] == policy["id"]:
                result[name] = _pointer(store, db, pointer["revision"], policy)
        return result


def publish_candidate(store, value):
    with store._db(write=True) as db:
        store._lock_head(db)
        body = _candidate(db, value)
        previous = _get(db, "candidate-published:" + value)
        if previous:
            return {**previous, "status": "already-landed"}
        head = store._head(db)
        if head != body["head"]:
            raise Conflict({"@head": {"kind": "head", "expected": body["head"],
                                      "actual": head}}, head=head)
        policy = _get(db, "validation-policy")
        if _checks(db, body["revision"], body["tree"], policy) is None:
            raise SeedError("Candidate has not passed the current validation policy")
        for proposal in body["proposals"]:
            store._land(db, proposal)
        if store._head(db) != body["revision"] or tree_id(_manifest(store, db, body["revision"])) != body["tree"]:
            raise SeedError("Published candidate differs from tested state")
        result = {"candidate": value, "revision": body["revision"], "status": "landed"}
        _immutable(db, "candidate-published:" + value, result)
        _advance_verified(store, db, body["revision"])
        return result


def promote(store, revision):
    identifier(revision)
    with store._db(write=True) as db:
        store._lock_head(db)
        pointer = _pointer(store, db, revision, _get(db, "validation-policy"))
        if pointer is None:
            raise SeedError("Revision has not passed the current validation policy")
        promotion = object_id("promotion", canonical(pointer))
        _immutable(db, "promotion:" + promotion, pointer)
        _set(db, "deployable", pointer)
        return {"promotion": promotion, **pointer}


def configuration_id(command, environment):
    return object_id("validation-config", canonical({"command": command, "environment": environment}))


class _SnapshotClient:
    def __init__(self, client, subject, files):
        self.client, self.subject, self.files = client, subject, files

    def head(self):
        return self.subject

    def range_snapshot(self, prefix="", kind="subtree", revision=None):
        from seed import _range_fingerprint, normalize_prefix
        prefix = normalize_prefix(prefix)
        files = {p: b for p, b in self.files.items() if p.startswith(prefix)}
        return {"revision": self.subject, "prefix": prefix, "kind": kind,
                "paths": sorted(files), "files": files,
                "fingerprint": _range_fingerprint(kind, sorted(files), files)}

    def read_blob(self, value):
        return self.client.read_blob(value)


def run_validation(client, *, kind, subject, workspace, command, environment, timeout=60):
    """Execute locally, with no publisher lock held; submit a trusted attestation."""
    from seed_agent import TaskFile, _new_state, _run, _session
    if kind == "candidate":
        snapshot = client.candidate(subject)
        files = snapshot["files"]
    elif kind == "revision":
        files = client.range_snapshot("", "subtree", subject)["files"]
    else:
        raise SeedError("Unknown validation subject kind")
    configuration = configuration_id(command, environment)
    adapter = _SnapshotClient(client, subject, files)
    state_file = TaskFile(str(Path(workspace).absolute()) + ".validation.json")
    with state_file.locked():
        state = _new_state(adapter, "http://localhost", "validation")
        state_file.create(state)
        session = _session(adapter, state)
        args = argparse.Namespace(workspace=workspace, prefix="", command=command,
                                  command_timeout=timeout)
        result = _run(args, state, session, state_file)
        # Generated outputs may be new files. Versioned inputs must be unchanged.
        modified_inputs = sorted(path for path in session.edits if path in files)
        passed = result["returncode"] == 0 and not result["timed_out"] and not modified_inputs
        elapsed = next(item["elapsed_ms"] for item in reversed(state["metrics"]) if item["event"] == "run")
        state_file.save(state)
    report = dict(kind=kind, subject=subject, configuration=configuration,
                  environment=hashlib.sha256(environment.encode()).hexdigest(),
                  passed=passed, returncode=result["returncode"], timed_out=result["timed_out"],
                  elapsed_ms=elapsed, log_digest=object_id("validation-log", canonical(result)),
                  request=uuid4().hex)
    validation = client.record_validation(**report)
    return {"validation": validation, "configuration": configuration,
            "modified_inputs": modified_inputs, "result": result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--repo", required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    config = commands.add_parser("config")
    config.add_argument("--environment", required=True)
    config.add_argument("argv", nargs=argparse.REMAINDER)
    policy = commands.add_parser("policy")
    policy.add_argument("configurations", nargs="+")
    pending = commands.add_parser("candidate")
    pending.add_argument("proposals", nargs="+")
    execute = commands.add_parser("run")
    execute.add_argument("--kind", choices=["candidate", "revision"], required=True)
    execute.add_argument("--subject", required=True)
    execute.add_argument("--workspace", required=True)
    execute.add_argument("--environment", required=True)
    execute.add_argument("--timeout", type=float, default=60)
    execute.add_argument("argv", nargs=argparse.REMAINDER)
    publish = commands.add_parser("publish")
    publish.add_argument("candidate")
    deploy = commands.add_parser("promote")
    deploy.add_argument("revision")
    commands.add_parser("state")
    try:
        args = parser.parse_args(argv)
        command = getattr(args, "argv", [])
        command = command[1:] if command[:1] == ["--"] else command
        if args.action in {"config", "run"} and not command:
            raise SeedError("Command is required after --")
        if args.action == "config":
            result = {"configuration": configuration_id(command, args.environment)}
        else:
            from seed_client import Client
            token = os.environ.get("SEED_TOKEN")
            if not token:
                raise SeedError("SEED_TOKEN is required")
            client = Client(args.url, args.repo, token)
            if args.action == "policy":
                result = client.set_validation_policy(args.configurations)
            elif args.action == "candidate":
                result = client.create_candidate(args.proposals)
            elif args.action == "run":
                result = run_validation(client, kind=args.kind, subject=args.subject,
                                        workspace=args.workspace, command=command,
                                        environment=args.environment, timeout=args.timeout)
            elif args.action == "publish":
                result = client.publish_candidate(args.candidate)
            elif args.action == "promote":
                result = client.promote(args.revision)
            else:
                result = client.validation_state()
        print(json.dumps(result, ensure_ascii=False))
        return 1 if args.action == "run" and not result["validation"]["report"]["passed"] else 0
    except (SeedError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
