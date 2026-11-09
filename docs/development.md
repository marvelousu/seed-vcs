# Development, tests and measurements

## Tests

Run the behavioral suite and demo from the repository directory:

```sh
python -m unittest -v test_seed.py test_predicates.py test_backup.BackupTests test_git.py test_comparison.py test_workflow_environment.py test_merge.py
python demo.py
```

Install service test dependencies and run the HTTP contracts:

```sh
python -m pip install httpx==0.28.1
python -m unittest -v test_service.ServiceTests test_session.py test_agent.py test_validation.ValidationTests
```

Set `SEED_TEST_DATABASE_URL` to a **dedicated test database** and run
`python -m unittest -v test_service.PostgresServiceTests test_validation.PostgresValidationTests test_backup.PostgresBackupTests`
to exercise the HTTP, validation and restore contracts on PostgreSQL. Tests
create and remove their own random schemas.
CI runs SQLite/core and service tests on Windows and Linux, PostgreSQL contracts
on Linux, and builds the container image.

The suite covers independent proposals, rejected writes with no partial
publication, declared read conditions, idempotent retries, historical reads,
namespace collisions, storage-failure rollback, and multiple writer processes.
It also covers scoped dependency conditions, work-view semantics, persistent
task recovery and command workspace safety. Legacy low-level calls still
demonstrate what happens when a dependency is not declared.

[GitHub Actions](https://github.com/marvelousu/seed-vcs/actions/workflows/test.yml)
runs the suite and demo on Ubuntu and Windows with Python 3.12. Tests and the
demo use temporary databases under `artifacts/tmp` and clean them up on exit.

## Load measurement

Use an empty repository with an authorized credential to measure the real HTTP
service. Load an initial repository, submit independent changes from concurrent
clients, then verify all file paths and revision sequence numbers:

```sh
seed-benchmark --repo bench --files 100000 --proposals 1000 --clients 64 --batch 8 --output result.json
```

The command reads `SEED_TOKEN`. Results include completed proposals, throughput,
batch latency percentiles, failures and the client environment. It measures
proposal creation and landing over HTTP, including blob uploads. Initial
repository loading and final verification are timed separately from submissions.

A [recorded run](benchmark.md) used 100,000 initial paths, four HTTP workers
and 64 concurrent clients: 1,000 proposals landed in 18.41 seconds with no errors.
The report includes the payload sizes, initial load time and latency percentiles.

## Comparisons with Git workflows

For a deterministic comparison with automated Git and Git with equivalent
dependency checks, run `python workflow_benchmark.py --output artifacts/workflow-replay.json`.
See the [workflow guide](agent-workflow.md#measure-before-extending-the-architecture)
for what this replay measures and how it differs from live coding runs.

The [agent comparison guide](agent-comparison.md) explains how captured
coding traces are replayed against Seed and automated Git workflows.
The [wide-directory publication experiment](wide-tree-benchmark.md) tests
local integration cost against a batched bare-Git importer, with small and
sharded controls.

The opt-in [live coding study](live-agent-study.md) runs fresh Claude Code
sessions on a small asset-review CLI using Seed, automated Git integration, and
Git with an equivalent application-scope condition. Its protocol, correctness
checks, repair limits, timing categories, and individual results are recorded
separately from deterministic replay benchmarks. Live model calls never run in CI.

## Contributing changes

For a bug report, include the Python version, operating system, a minimal
sequence of commands, and the expected and actual result. Use small, synthetic
file contents for reproductions rather than uploading a database containing
private code.

For changes to landing or history behavior, include a regression test and
describe any effects on existing databases or proposal identity. Please keep
the core dependency-free and run the test commands above before submitting a
pull request.
