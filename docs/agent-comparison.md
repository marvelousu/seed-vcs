# Recorded-agent paired comparison

`agent_comparison.py` implements the Stage 5 pilot. It replays a recorded
candidate against Seed, an automated Git worktree, and a Git worktree with the
same captured dependency checks. It does not invoke a model or claim to measure
live-agent productivity.

```sh
python agent_comparison.py replay --trace artifacts/real-agent-pilot.json \
  --repeats 5 --output artifacts/agent-comparison.json
```

Each input has `format: "seed-agent-comparison-trace/1"` and exactly one of
each pilot group: `independent-edit`, `shared-file-disjoint-lines`, and
`changed-read-input`.

```json
{
  "format": "seed-agent-comparison-trace/1",
  "trace_id": "agent-run-2026-10-20",
  "recording": {"kind": "real-agent-cli", "label": "pricing pilot"},
  "workloads": [{
    "id": "independent-1",
    "group": "independent-edit",
    "initial": {"src/a.py": {"encoding": "utf-8", "data": "old\\n"}},
    "background": {"docs/a.md": {"encoding": "utf-8", "data": "changed\\n"}},
    "agent": {
      "actions": [{"op": "read", "path": "src/a.py"}],
      "proposal": {
        "id": "optional-recorded-proposal-id",
        "edits": {"src/a.py": {"encoding": "utf-8", "data": "candidate\\n"}},
        "observed": {"reads": ["src/a.py"], "ranges": []}
      },
      "rework": [],
      "model": {"input_tokens": null, "output_tokens": null, "cost_usd": null},
      "transport": {"requests": null, "upload_bytes": null, "download_bytes": null, "request_seconds": null}
    },
    "oracle": {"expected_safe": true, "reason": "The concurrent document edit is independent."}
  }]
}
```

Payloads use explicit UTF-8 or base64 encoding. `background` and proposal
`edits` may use `null` for deletion. The oracle is deliberately separate from
the recorded read/range observations: observations are what the agent captured,
not a safety judgment. Inputs are bounded to 1,000 files, 1 MiB per payload,
and 4 MiB per trace by default.

The result records accepts, unsafe accepts, conservative rejections, captured
reads/ranges, recorded rework count, replay rework-required count, phase time,
and available transport data. Seed uses the in-process store in this first
harness, so request-byte counters are `null`; Git counters are zero. Rejected
fixed replays do not fabricate a new model answer. Five replay repetitions are
paired integration measurements, not five independent live-agent samples.

`recording.kind` must be `real-agent-cli` or `real-agent-client` for ordinary
comparison. Use `real-agent-client` when a coding agent recorded direct
`Client`/`Session` operations instead of the persistent CLI; state that exact
method in its label. Fixture input requires `--allow-fixture` and is labelled as
such in output. The existing
`artifacts/agent-pilot-20261020/task.json` records useful real-pilot reads,
ranges, edits, and command metrics, but lacks all three groups and independent
base/background/oracle facts; it is not a complete comparison trace.

[`examples/comparison-live-20261020.json`](../examples/comparison-live-20261020.json)
is a sanitized transcription of one actual three-workload coding run. Its test
actions use `python` and replace local workspace paths with `<workspace>`; the
initial code, concurrent edits, observed reads, candidate edits, conflict and
rework outcomes remain intact. It is one live sample per workload. Reuse it
with the replay command above, but do not treat five replay repetitions as five
model runs.

## Generic external recorder

Prepare a trace envelope with fixed `initial`, `background`, and `oracle`, then
run an explicitly chosen recorder:

```sh
python agent_comparison.py capture --spec artifacts/spec.json \
  --output artifacts/real-agent-pilot.json --timeout 120 -- \
  path/to/recorder --spec {spec}
```

The recorder gets the spec path in `SEED_COMPARISON_SPEC` and by `{spec}`
substitution. It writes one JSON object to stdout:

```json
{
  "format": "seed-agent-comparison-adapter-result/1",
  "workloads": [{
    "id": "same-id-as-spec",
    "actions": [],
    "proposal": {"id": null, "edits": {}, "observed": {"reads": [], "ranges": []}},
    "rework": [],
    "model": {"input_tokens": null, "output_tokens": null, "cost_usd": null},
    "transport": {"requests": null, "upload_bytes": null, "download_bytes": null, "request_seconds": null}
  }]
}
```

Capture uses `shell=False`, has an explicit timeout, strips `SEED_TOKEN`, and
validates that the recorder did not alter the independent base, background, or
oracle. It never chooses or pays for a model. To finish the initial 3 x 5 pilot,
record one real coding trace containing all three workload groups, then replay
its fixed outputs five times and report replay repetitions separately from the
number of real agent runs.

## Initial captured-sample result

The [captured example](../examples/comparison-live-20261020.json) contains one
coding sample for each of the three groups, recorded through `Client`/`Session`
and the HTTP contract. The examples are small pricing tasks; they are not a
representative production workload. Model tokens and cost were unavailable and
remain `null`.

[CI run 39364467645](https://github.com/marvelousu/seed-vcs/actions/runs/39364467645)
replayed each fixed result five times on Linux, producing 15 observations per
backend (45 total). These are repetitions of three captured changes, not 15
independent coding sessions.

| Backend | Accepted | Stale-input accepts | Conservative rejections |
| --- | ---: | ---: | ---: |
| Seed | 5 / 15 | 0 / 15 | 5 / 15 |
| Git worktree and merge | 15 / 15 | 5 / 15 | 0 / 15 |
| Git worktree with captured dependency checks | 5 / 15 | 0 / 15 | 5 / 15 |

The stale-input column uses the trace's declared oracle: the derived quote was
computed before its input changed. It does not imply that ordinary Git promises
to check runtime dependencies. Seed and the conditional Git workflow made the
same decisions. Both conservatively rejected the independent line edit in a
shared file, and both required reintegration for two of the three groups.

The live trace records successful reread/rework after those conflicts. The paired
replay evaluates the original fixed outputs rather than asking the model to
solve each conflict again. Timing is recorded in the downloadable CI artifact,
but the in-process Seed backend and filesystem Git backends do not establish a
service throughput, end-to-end cost, or productivity advantage. The result
supports the dependency-check behavior and its conservative tradeoff; it does
not establish that a new VCS is necessary instead of a Git-based agent adapter.