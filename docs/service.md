# Running the service and using the Python client

This page covers the HTTP service, its configuration and limits, the Python
client, and the container setup. For the restartable `seed-agent` command line,
see the [agent workflow guide](agent-workflow.md).

## Run the service

Install the service and create a credential for a repository:

```sh
python -m pip install -r requirements-server.txt
python -m pip install --no-deps -e .
seed-server token --repo demo
seed-server serve
```

The `token` command prints the bearer credential once and adds its SHA-256
digest to `.seed/credentials.json`. Keep the credential in your secret manager;
the service reads only the digest file. Restart workers after updating grants.
The default listener is `127.0.0.1:7331`. Without `SEED_DATABASE_URL`, repositories
use SQLite files under `.seed/repos`.

For PostgreSQL, supply `SEED_DATABASE_URL` through your secret manager or process
configuration, then start multiple workers:

```sh
seed-server serve --workers 4
```

The PostgreSQL account must be able to create schemas in the selected database.
Workers use PostgreSQL for metadata and `.seed/objects` for file bytes. All
workers and hosts serving the same repositories must share that object volume.
Serve remote connections through an HTTPS reverse proxy; remote HTTP is rejected
by the client unless explicitly enabled.

## Configuration and limits

| Configuration | Default | Purpose |
| --- | --- | --- |
| `SEED_DATA` | `.seed` | Object directory and local SQLite repositories. |
| `SEED_AUTH_FILE` | `$SEED_DATA/credentials.json` | Repository grants and credential digests. |
| `SEED_DATABASE_URL` | unset | PostgreSQL connection string; unset selects SQLite. |
| `SEED_POOL_SIZE` | `16` | Maximum PostgreSQL connections per worker. |

Requests are limited to 8 MiB, proposals to 1,000 write paths and 1,000 read
paths, landing batches to 256 proposals, and listing pages to 1,000 items.
The server also limits concurrent connections and queued database acquisitions.

A bearer credential can be restricted to named repositories and to read, write,
create, validate or promote permissions. File and revision listings use
cursor-based pages.

## Python client

The client uses Python's standard library. Pass your credential from the
environment populated by your secret manager:

```python
import os
from seed_client import Client

client = Client("http://127.0.0.1:7331", "demo", os.environ["SEED_TOKEN"])
client.create()  # Idempotent repository creation; requires create permission.

task = client.begin()
previous = task.read("greeting.txt")  # Read condition is captured automatically.
task.write("greeting.txt", b"Hello from an agent!\n")
proposal = task.propose(author="agent-1", message="Update greeting")
print(client.land(proposal))
```

In version 0.3, `task.read()` reads the pinned base **plus pending edits**.
Use `task.read_base()` to read the original pinned version explicitly. An absent
file is captured as an absence condition. `task.write(path, None)` proposes
deletion. `task.files("src/")` records directory membership, while
`task.search("TODO", "src/")` guards all paths and contents in that subtree,
including files which did not match. Scopes are bounded to 10,000 files and
proposals to 64 range conditions; oversized scopes fail rather than truncate
their dependency conditions. Search output is bounded separately.

`Client.land_many(ids)` returns one result per proposal. Conflicts and namespace
errors do not undo other successful proposals; a database failure rolls back
the whole batch. HTTP 409 reports changed conditions. Temporary connection
errors and HTTP 429/503 are retried up to three times. Replaying a successful
landing returns `already-landed` and the original revision.

## Containers

The included Compose configuration starts PostgreSQL and four service workers.
Create `.seed/credentials.json` with the token command first, supply
`POSTGRES_PASSWORD` through your secret manager, and run:

```sh
docker compose up --build -d
```

The port binds to loopback. Metadata and objects use persistent volumes. The
application runs as a non-root user. `/healthz` checks the process and `/readyz`
checks database availability. Back up PostgreSQL and its referenced objects
together. Uploaded objects are retained; the service does not delete them.

## Related pages

- [Validation and deployment targets](validation.md)
- [One-way Git bridge](git-bridge.md)
- [Backup and restore](backup.md)
- [Architecture](architecture.md)
