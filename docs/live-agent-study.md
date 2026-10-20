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
