"""Authenticated, bounded HTTP service for concurrent Seed clients."""

import argparse
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from seed import Conflict, SeedError, Store, file_path, object_id


REPOSITORY = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
IDENTIFIER = r"^[0-9a-f]{64}$"
BODY_LIMIT = 8 * 1024 * 1024


class Auth:
    def __init__(self, path):
        self.entries = json.loads(Path(path).read_text(encoding="utf-8"))
        self.validate()

    @classmethod
    def from_entries(cls, entries):
        auth = cls.__new__(cls)
        auth.entries = entries
        auth.validate()
        return auth

    def validate(self):
        if not isinstance(self.entries, list) or not self.entries:
            raise ValueError("Auth file must contain a nonempty list")
        for entry in self.entries:
            if not re.fullmatch(IDENTIFIER, entry.get("sha256", "")):
                raise ValueError("Invalid credential digest")
            repos = entry.get("repositories", [])
            if not repos or any(not re.fullmatch(REPOSITORY, r) for r in repos):
                raise ValueError("Credentials must name their repositories")
            if not set(entry.get("permissions", [])).issubset(
                {"read", "write", "create"}
            ):
                raise ValueError("Invalid credential permissions")

    def check(self, authorization, repository, permission):
        if not re.fullmatch(REPOSITORY, repository):
            raise HTTPException(422, "Invalid repository name")
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer credential required")
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        matched = [
            entry
            for entry in self.entries
            if hmac.compare_digest(digest, entry["sha256"])
        ]
        if not matched:
            raise HTTPException(401, "Invalid credential")
        if not any(
            repository in e["repositories"] and permission in e["permissions"]
            for e in matched
        ):
            raise HTTPException(403, "Repository permission denied")


class Provider:
    def __init__(self, root, dsn=None, pool_size=16):
        self.root = Path(root).resolve()
        (self.root / "repos").mkdir(parents=True, exist_ok=True)
        self.pool = None
        if dsn:
            from seed_postgres import make_pool

            self.pool = make_pool(dsn, pool_size)
        self.stores = {}
        self.lock = threading.Lock()

    def repository(self, name, create=False):
        if not re.fullmatch(REPOSITORY, name):
            raise SeedError("Invalid repository name")
        with self.lock:
            if name in self.stores:
                return self.stores[name]
            if self.pool:
                from seed_postgres import PostgresStore

                if create:
                    store = PostgresStore.create(self.pool, name, self.root / "objects")
                else:
                    store = PostgresStore(self.pool, name, self.root / "objects")
                    with self.pool.connection() as connection:
                        row = connection.execute(
                            "SELECT 1 FROM information_schema.schemata WHERE schema_name=%s",
                            (store.schema,),
                        ).fetchone()
                    if row is None:
                        raise HTTPException(404, "Repository not found")
            else:
                path = (
                    self.root
                    / "repos"
                    / (hashlib.sha256(name.encode()).hexdigest() + ".db")
                )
                if not path.exists():
                    if not create:
                        raise HTTPException(404, "Repository not found")
                    try:
                        store = Store.create(path)
                    except SeedError:
                        store = Store(path)
                else:
                    store = Store(path)
            self.stores[name] = store
            return store

    def close(self):
        if self.pool:
            self.pool.close()


class BodyLimit:
    def __init__(self, app, auth, limit=BODY_LIMIT):
        self.app = app
        self.auth = auth
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        route = re.fullmatch(r"/v1/repos/([^/]+)(/.*)?", scope["path"])
        if route:
            permission = (
                "create"
                if scope["method"] == "POST" and not route[2]
                else "read"
                if scope["method"] in {"GET", "HEAD"}
                else "write"
            )
            try:
                self.auth.check(
                    headers.get(b"authorization", b"").decode("latin1"),
                    route[1],
                    permission,
                )
            except HTTPException as exc:
                return await JSONResponse(
                    {"detail": exc.detail}, status_code=exc.status_code
                )(scope, receive, send)
        if scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse(
                {"detail": "Invalid Content-Length"}, status_code=400
            )(scope, receive, send)
        if declared < 0 or declared > self.limit:
            return await JSONResponse({"detail": "Request too large"}, status_code=413)(
                scope, receive, send
            )
        chunks = []
        size = 0
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            size += len(event.get("body", b""))
            if size > self.limit:
                return await JSONResponse(
                    {"detail": "Request too large"}, status_code=413
                )(scope, receive, send)
            chunks.append(event.get("body", b""))
            if not event.get("more_body", False):
                break
        body = b"".join(chunks)
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    base: str = Field(pattern=IDENTIFIER)
    writes: dict[str, str | None] = Field(min_length=1, max_length=1000)
    reads: list[str] = Field(default_factory=list, max_length=1000)
    author: str = Field(default="", max_length=256)
    message: str = Field(default="", max_length=4096)


class Landing(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    proposal: str = Field(pattern=IDENTIFIER)


class Batch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    proposals: list[str] = Field(min_length=1, max_length=256)


def create_app(*, root=None, auth_file=None, auth=None, dsn=None, pool_size=None):
    root = root or os.environ.get("SEED_DATA", ".seed")
    auth_file = auth_file or os.environ.get(
        "SEED_AUTH_FILE", str(Path(root) / "credentials.json")
    )
    dsn = dsn if dsn is not None else os.environ.get("SEED_DATABASE_URL")
    pool_size = pool_size or int(os.environ.get("SEED_POOL_SIZE", "16"))
    auth = auth or Auth(auth_file)
    provider = Provider(root, dsn, pool_size)

    @asynccontextmanager
    async def lifespan(app):
        try:
            if provider.pool:
                provider.pool.wait(timeout=15)
            yield
        finally:
            provider.close()

    app = FastAPI(title="Seed", version="0.2.0", lifespan=lifespan)
    app.add_middleware(BodyLimit, auth=auth)
    app.state.provider = provider

    def access(permission):
        def dependency(repo: str, authorization: str | None = Header(default=None)):
            auth.check(authorization, repo, permission)
            return repo

        return dependency

    read_access, write_access, create_access = (
        access("read"),
        access("write"),
        access("create"),
    )

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse(
            {"status": "conflict", "paths": exc.differences}, status_code=409
        )

    @app.exception_handler(SeedError)
    async def error_handler(request, exc):
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=422)

    @app.exception_handler(sqlite3.DatabaseError)
    async def sqlite_handler(request, exc):
        return JSONResponse(
            {"status": "error", "message": "Storage operation failed"}, status_code=503
        )

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/readyz")
    def ready():
        if provider.pool:
            with provider.pool.connection() as connection:
                connection.execute("SELECT 1")
        return {"status": "ready"}

    @app.post("/v1/repos/{repo}")
    def create_repository(repo: str = Depends(create_access)):
        return {"revision": provider.repository(repo, create=True).head()}

    @app.get("/v1/repos/{repo}/head")
    def head(repo: str = Depends(read_access)):
        return {"revision": provider.repository(repo).head()}

    @app.put("/v1/repos/{repo}/blobs/{identifier}")
    async def upload(
        identifier: str, request: Request, repo: str = Depends(write_access)
    ):
        if not re.fullmatch(IDENTIFIER, identifier):
            raise HTTPException(422, "Invalid blob ID")
        payload = await request.body()
        if object_id("blob", payload) != identifier:
            raise HTTPException(422, "Blob hash does not match bytes")
        # Disk and database operations must not block the ASGI event loop.
        from starlette.concurrency import run_in_threadpool

        result = await run_in_threadpool(
            lambda: provider.repository(repo).put_blob(payload)
        )
        return {"blob": result}

    @app.get("/v1/repos/{repo}/blobs/{identifier}")
    def download(identifier: str, repo: str = Depends(read_access)):
        if not re.fullmatch(IDENTIFIER, identifier):
            raise HTTPException(422, "Invalid blob ID")
        return Response(
            provider.repository(repo).get_blob(identifier),
            media_type="application/octet-stream",
        )

    @app.get("/v1/repos/{repo}/file")
    def read_file(
        path: str = Query(max_length=2048),
        revision: str | None = None,
        repo: str = Depends(read_access),
    ):
        path = file_path(path)
        snapshot = provider.repository(repo).snapshot([path], revision)
        return {"revision": snapshot["revision"], "blob": snapshot["files"][path]}

    @app.get("/v1/repos/{repo}/files")
    def files(
        revision: str | None = None,
        after: str = Query(default="", max_length=2048),
        limit: int = Query(default=100, ge=1, le=1000),
        repo: str = Depends(read_access),
    ):
        return provider.repository(repo).file_page(
            revision=revision, after=after, limit=limit
        )

    @app.get("/v1/repos/{repo}/history")
    def history(
        after: int = Query(default=-1, ge=-1),
        limit: int = Query(default=100, ge=1, le=1000),
        repo: str = Depends(read_access),
    ):
        return provider.repository(repo).log_page(after=after, limit=limit)

    @app.post("/v1/repos/{repo}/proposals")
    def propose(body: Proposal, repo: str = Depends(write_access)):
        for path in set(body.writes) | set(body.reads):
            if len(path) > 2048:
                raise HTTPException(422, "File path too long")
            file_path(path)
        if any(
            blob is not None and not re.fullmatch(IDENTIFIER, blob)
            for blob in body.writes.values()
        ):
            raise HTTPException(422, "Invalid blob ID")
        proposal = provider.repository(repo).propose_refs(
            body.base,
            body.writes,
            reads=body.reads,
            author=body.author,
            message=body.message,
        )
        return {"proposal": proposal}

    @app.get("/v1/repos/{repo}/proposals/{identifier}")
    def describe(identifier: str, repo: str = Depends(read_access)):
        return provider.repository(repo).describe(identifier)

    @app.post("/v1/repos/{repo}/land")
    def land(body: Landing, repo: str = Depends(write_access)):
        return provider.repository(repo).land(body.proposal)

    @app.post("/v1/repos/{repo}/land-batch")
    def land_batch(body: Batch, repo: str = Depends(write_access)):
        if any(not re.fullmatch(IDENTIFIER, p) for p in body.proposals):
            raise HTTPException(422, "Invalid proposal ID")
        return {"results": provider.repository(repo).land_many(body.proposals)}

    if dsn:
        import psycopg
        from psycopg_pool import PoolTimeout, TooManyRequests

        async def unavailable(request, exc):
            return JSONResponse(
                {"status": "error", "message": "Storage unavailable; retry request"},
                status_code=503,
            )

        for error in (psycopg.Error, PoolTimeout, TooManyRequests):
            app.add_exception_handler(error, unavailable)
    return app


def issue_token(path, repo, permissions):
    if not re.fullmatch(REPOSITORY, repo):
        raise ValueError("Invalid repository name")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    # Lock a stable sidecar inode while replacing the credentials file atomically.
    import tempfile

    lock_path = target.with_suffix(target.suffix + ".lock")
    with lock_path.open("a+b") as lock:
        # Windows can lock a byte beyond EOF. Do not read or write the byte
        # before locking: another issuer may already own that range.
        lock.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            entries = (
                json.loads(target.read_text(encoding="utf-8"))
                if target.exists()
                else []
            )
            entries.append(
                {
                    "sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "repositories": [repo],
                    "permissions": permissions,
                }
            )
            Auth.from_entries(entries)
            descriptor, temporary = tempfile.mkstemp(dir=target.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(entries, stream, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Digests are readable by the service; only the owner can change
                # this file. Bearer tokens are never written into it.
                os.chmod(temporary, 0o644)
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return token


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    token = commands.add_parser("token")
    token.add_argument("--auth-file", default=".seed/credentials.json")
    token.add_argument("--repo", required=True)
    token.add_argument(
        "--permissions",
        nargs="+",
        choices=["read", "write", "create"],
        default=["read", "write", "create"],
    )
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7331)
    serve.add_argument("--workers", type=int, default=1)
    args = parser.parse_args(argv)
    if args.command == "token":
        print(issue_token(args.auth_file, args.repo, args.permissions))
    else:
        import uvicorn

        uvicorn.run(
            "seed_server:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            workers=args.workers,
            limit_concurrency=128,
            timeout_keep_alive=5,
            timeout_graceful_shutdown=15,
        )


if __name__ == "__main__":
    main()
