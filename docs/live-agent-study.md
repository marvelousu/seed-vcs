# Live coding study: ReviewBox

This opt-in experiment asks fresh coding agents to extend a small, usable asset
review CLI. It measures development, tests, integration, and bounded rework.
It does not replay previously recorded edits. The protocol below is fixed before
the first study session; launcher preflight and controller fixture tests are separate.

## Question and scope

Can Seed's existing scoped workspace runner change the amount or kind of work
needed to deliver a correct feature when another contributor changes the base?
How does that compare with automated Git integration, and Git with the same
conservative scope condition?

Each of three independent task cards starts from the same ReviewBox baseline:

| Card | Requested feature | Peer update |
| --- | --- | --- |
| clean-filter | Filter the JSON asset list by validated status | None |
| docs-export | Export all asset fields to correctly escaped UTF-8 CSV | Unrelated usage documentation |
| config-priority | Rank assets using runtime configuration; count all statuses | Rules schema and weights change, with documentation |

The peer update is applied deterministically after the candidate is produced and
before integration. The candidate works on its pinned old base. This is a
controlled concurrent-edit scenario, not an estimate of real-world conflict rates.
The documentation case deliberately measures the cost of conservative rejection.
An implementation that already handles the changed rules correctly counts as a
success for Git, without forcing a repair.

## Three workflows

- **Seed:** authenticated localhost HTTP server, SQLite backend, actual
  `seed-agent begin/run/propose/land/rework` commands. The normal-shell runner
  conservatively observes the whole application scope.
- **Git:** local worktree and branch, commit, automated three-way merge into the
  current main branch, then integrated public tests/probes.
- **Conditional Git:** the same Git workflow, plus a persisted whole-application
  base/current condition check before merging. This matches the runner's scope.

All files in the Seed repository are under `app/`; the runner observes repository
root and materializes the same `app/` layout used by Git. This does not trace
individual native file reads, and cannot claim fine-grained dependency detection.
The conditional Git guard also covers additions, deletions, and file contents.

Each initial attempt uses a fresh Claude Code session with normal file tools and
the same Python unittest command. Settings, hooks, memory, plugins, and MCP
discovery are disabled for these sessions. The CLI uses `--safe-mode --restricted`
to retain the existing subscription login while avoiding user/project
customizations. Bash is available for the explicit test command; ordinary file
tools do the implementation. This is a trusted local coding experiment, not an
adversarial sandbox evaluation.

The selected model is `claude-opus-5`, effort `medium`, at most 24 turns and
360 seconds per session. Resolved model identity is recorded. No model fallback,
package installation, deployment, or paid API credential is introduced.

## Integration and repair

Each arm gets at most one fresh repair session if integration rejects/conflicts,
or integrated public tests/probes fail. The repair starts from the current head
and receives the original task, current peer context, and generic integration
diagnostics. It must retain peer changes. Human-authored corrections are not
inserted into candidates. Final behavioral checks run outside the agent's
working directory and do not supply hidden expected answers to the model.

The same frozen requirements and evaluators apply to all arms. An agent's own
tests are retained and run at the integrated head. Candidate and final sources
are retained, including failed attempts. Missing/invalid model output is a
failure, not silently discarded. A launch/infrastructure failure may be retried
once only if no actual model content was emitted; it is recorded separately.

The fixed order counterbalances arms across cards:

1. clean-filter: Seed, Git, conditional Git.
2. docs-export: Git, conditional Git, Seed.
3. config-priority: conditional Git, Seed, Git.

Runs are sequential. There are nine initial sessions, each with at most one repair.
This is a case-study pilot with one observation per cell, not a statistically
powered experiment. Model latency, sampling variation, order, and cache reuse
can affect elapsed time; timings do not establish a general productivity ratio.

## Records

Freeze records include source, baseline, peer, prompt, and evaluator hashes.
Raw session events and disposable credentials stay under ignored `artifacts/`.
Public records include sanitized results and implemented source files.

Report final correctness, integration decisions, reasons for repairs, session
count, end-to-end and model wall time, tool/test activity, and token usage.
Input, cache-read, cache-creation, and output tokens are separate categories.
CLI `total_cost_usd`, when present, is an **estimated API list-price equivalent**;
it is not the amount charged to the subscription. Missing telemetry is null.
Controller/server/test overhead is not attributed to model reasoning.

## Reproduction

The live command uses your existing Claude Code login and consumes its usage
allowance. It is never part of CI. From the repository root, with server/test
dependencies installed:

```powershell
.venv/Scripts/python.exe -B -m unittest -v experiments.live_agent.test_taskpack experiments.live_agent.test_run_study
.venv/Scripts/python.exe -B -m experiments.live_agent.run_study --output artifacts/live-agent-study --adapter experiments/live_agent/claude_adapter.py --python D:/path/to/seed-vcs/.venv/Scripts/python.exe
```

`--dry-run` only writes the freeze/plan and performs no development. It is not
the controller smoke test or a live coding measurement.

The launcher flags follow the [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference).
Launcher preflight revealed that bare mode did not retain the local OAuth login;
the frozen live launcher uses safe mode instead. That preflight is not a study
sample.

Git operations are performed by the coordinator, not the coding model. This
controls integration automation and does not compare different agents' Git
command skills. The coding prompt forbids Git commands in every arm.

## Observations: 2026-10-20

All nine cells completed and passed the final public tests and external
behavioral checks. There were nine initial sessions and five repairs: **14 fresh
Claude Opus 5 sessions**, with no discarded live attempts or infrastructure
reruns. The launcher-reported model matched `claude-opus-5` in every session.
The executed controller, taskpack, and launcher hashes still matched their
pre-run freeze after the last cell. The frozen source commit was
`347ab8f41f990901b3f3f53bd9613bf0a4a3b4ce`.
Runtime: Windows, Python 3.13.14, Git 2.54.0.windows.1, Claude Code 2.1.280.

The [public evidence archive](measurements/live-agent-20261020.json.gz) retains
all nine outcomes, 14 telemetry summaries, candidate/final source trees, the
source freeze, and Seed CLI transport counters. Raw event streams remain local;
paths and raw model reports are removed from the public export. Source records
distinguish original-byte hashes from hashes of the sanitized exported text.
The 25,593-byte gzip archive has SHA-256
`0d8378f5c4a3fcea2696c76391583230a0dbcbfeb36f180706d48483da3b47d5`.

### Correctness and repairs

| Card | Seed | Automated Git | Conditional Git |
| --- | --- | --- | --- |
| Filter, no peer edit | Pass, no repair | Pass, no repair | Pass, no repair |
| CSV, unrelated docs edit | Scope rejection; one repair; pass | Merge and tests pass; no repair | Scope rejection; one repair; pass |
| Priority, rules migration | Scope rejection; one repair; pass | Merge succeeds, integrated tests fail; one repair; pass | Scope rejection; one repair; pass |

In the priority Git case, the first agent's added tests also detected the
migration: eight tests ran and six errored after integration. The repair updated
the implementation for schema v2 and migrated the agent-added runtime-policy
test. The originally supplied `tests/test_base.py` stayed unchanged. New task
tests were retained rather than deleted to make the result pass.

The peer changes and the original fixture/base-test bytes were preserved in
every final result. Candidate and integrated-head checks were separate; a
successful Git merge alone was never a completed feature.

### Elapsed time

The following numbers are **launcher wall seconds**, summed across initial and
repair sessions. They include model calls and the model's file/test tools, but
exclude controller setup, publication, and external grading. These are single
observations, not medians or a stable speed ranking.

| Card | Seed | Automated Git | Conditional Git |
| --- | ---: | ---: | ---: |
| Filter | 59.05 | 54.90 | 78.00 |
| CSV with docs edit | 133.08 | 68.38 | 141.30 |
| Priority with rules migration | 159.26 | 256.95 | 146.41 |
| Sum of three cards | 351.39 | 380.23 | 365.71 |

| Other totals | Seed | Automated Git | Conditional Git |
| --- | ---: | ---: | ---: |
| Initial + repair sessions | 5 | 4 | 5 |
| Whole study-cell wall seconds | 430.91 | 507.18 | 405.60 |
| CLI-reported API duration seconds | 320.543 | 320.309 | 310.158 |
| Output tokens | 26,508 | 23,258 | 23,096 |
| Noncached input tokens | 82 | 78 | 86 |
| Cache creation input tokens | 46,174 | 47,310 | 44,236 |
| Cache read input tokens | 449,838 | 534,337 | 482,552 |
| CLI estimated API list-price equivalent (USD) | 1.349769 | 1.322109 | 1.261466 |

The last row is the CLI's estimate, **not a subscription charge**. The study used
the existing subscription login. Cached and noncached token categories are
reported separately rather than treated as interchangeable work units.

Whole-cell time includes fixture/controller setup, candidate checks, integration,
public tests, actual-head materialization for Seed, and final external checks.
It is not a pure VCS latency measurement. The Windows host was not isolated from
other work. Model latency, test design, process startup, and filesystem delays
all contribute to these timings. For example, the local no-model controller
check took substantially longer than its CI execution. The API duration totals
are nearly equal despite differences in launcher and whole-cell wall time.

### What this supports

The live workflow worked: agents wrote new implementation and tests, submitted
their changes, repaired from a changed head when needed, and delivered passing
CLI behavior. Seed's conditions stopped the rules migration case before
publication. Git with an equivalent scope condition made the same decisions.

The scope condition also caused real extra work for an unrelated documentation
change. The current runner observes the whole app, so it cannot distinguish
that update from a relevant input change. Both guarded workflows started fresh
from the current head after rejection; they did not automatically reapply the
rejected candidate patch. Automated patch reuse could reduce that repair cost.
The normal Git repair started from the already-merged feature and its tests.
This is a comparison of these concrete workflows, not an intrinsic lower bound
on either VCS's repair effort.

The priority Seed run finished sooner than the normal Git run in this sample,
but the generated implementations/tests and model timings differed. That does
not isolate a causal speed benefit from Seed. Likewise, normal Git's post-merge
test failure does not show that Git must publish broken code: a merge queue can
test the combined candidate before advancing a shared branch.

**This pilot does not establish a practical advantage over automated Git or a
need for an independent VCS.** It provides an auditable small development example
of condition checks, their conservative rejection cost, and test-based repair.
Fine-grained read capture, many-agent development, larger applications, and
repeated productivity measurements remain outside this experiment's evidence.
