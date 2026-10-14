# HTTP load measurement

Measured on 2026-10-15 using the included `seed-benchmark` workload.

| Configuration | Value |
| --- | --- |
| Client and server host | Windows 11, 16 logical CPUs |
| Python | 3.13.14 |
| Metadata | PostgreSQL 18.6, durable commits enabled |
| HTTP workers | 4 |
| Database pool | Up to 16 connections per worker |
| Transport | Loopback HTTP |
| Initial paths | 100,000 |
| Submitted proposals | 1,000 independent one-file changes |
| Concurrent client tasks | 64 |
| Landing batch | 8 proposals |

The initial files contain the same eight-byte payload, `initial\n`, and share
one content-addressed blob. Each submitted change writes its decimal task number
to a new, distinct path. This workload exercises path metadata, concurrent
submission, blob uploads and ordered landing, rather than large binary transfer.
All proposals use the same base revision.

| Result | Value |
| --- | --- |
| Initial repository load | 332.82 seconds |
| Submission and landing | 18.41 seconds |
| Completed proposals | 1,000 |
| Throughput | 54.31 proposals/second |
| Batch p50 latency | 7.93 seconds |
| Batch p95 latency | 11.57 seconds |
| Batch p99 latency | 11.99 seconds |
| Errors | 0 |
| Final file count | 101,000 |
| Final revision count | 1,101 |

Throughput includes proposal creation, changed-blob uploads and landing. Batch
latency starts when a client task starts creating its eight proposals and ends
when the landing response arrives; it includes contention from other clients.
Initial loading and final verification are outside the submission timer.

Final verification paged through every path and revision, checked the expected
file count and contiguous revision sequence, and read a sample of submitted
file contents. The test service was then stopped. No Git comparison was run.

## Reproduce

Start the service with PostgreSQL and four workers, create a credential scoped
to a new empty repository, and inject it as `SEED_TOKEN`:

```sh
seed-benchmark --repo bench --files 100000 --proposals 1000 --clients 64 --batch 8 --output result.json
```

Change `--files`, `--proposals`, `--clients` and `--batch` to compare workload
sizes and batching on the same environment. The command requires an empty
repository and does not overwrite an existing repository's files.
