"""Build-job smoke test of the non-root image against the job's PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import urllib.request

from seed_client import Client


def command(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def main():
    root = Path("artifacts")
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        config = Path(directory).resolve() / "credentials.json"
        token = secrets.token_urlsafe(32)
        repo = "container_" + secrets.token_hex(6)
        config.write_text(
            json.dumps(
                [
                    {
                        "sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "repositories": [repo],
                        "permissions": ["read", "write", "create"],
                    }
                ]
            )
        )
        config.chmod(0o644)
        name = "seed-smoke-" + secrets.token_hex(6)
        volume = name + "-objects"
        os.environ["SEED_DATABASE_URL"] = os.environ["SEED_TEST_DATABASE_URL"]
        command("volume", "create", volume)
        try:
            command(
                "run",
                "--detach",
                "--rm",
                "--name",
                name,
                "--network",
                "host",
                "--env",
                "SEED_DATABASE_URL",
                "--env",
                "SEED_AUTH_FILE=/run/seed/credentials.json",
                "--mount",
                f"type=bind,src={config},dst=/run/seed/credentials.json,readonly",
                "--mount",
                f"type=volume,src={volume},dst=/data",
                "seed-service",
            )
            deadline = time.monotonic() + 45
            while True:
                try:
                    urllib.request.urlopen(
                        "http://127.0.0.1:7331/readyz", timeout=1
                    ).close()
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise RuntimeError("Container readiness failed")
                    time.sleep(0.2)
            client = Client("http://127.0.0.1:7331", repo, token)
            base = client.create()["revision"]

            def submit(i):
                worker = Client("http://127.0.0.1:7331", repo, token)
                proposal = worker.propose(base, {f"files/{i}": str(i).encode()})
                return worker.land(proposal)["status"]

            with ThreadPoolExecutor(max_workers=8) as pool:
                assert list(pool.map(submit, range(16))) == ["landed"] * 16
            assert len(client.files()["files"]) == 16
            assert client.read("files/7") == b"7"
            print("Container HTTP/PG/volume smoke passed: 16 concurrent proposals")
        finally:
            subprocess.run(
                ["docker", "stop", "--time", "10", name], capture_output=True
            )
            command("volume", "rm", volume)


if __name__ == "__main__":
    main()
