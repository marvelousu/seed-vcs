# Validation and deployment targets

Seed keeps three distinct states: the integrated `head`, a `verified` revision,
and an explicitly promoted `deployable` revision. Normal landing continues to
check dependency conditions without waiting for tests. A worker can validate
that exact revision later. Failed or untested newer heads do not advance the
deployment target.

Validation is an attestation from a trusted worker. The service does not run
commands, prove that a worker executed them, or infer correctness from an agent's
write permission. Issue the worker `read validate` permissions and the operator
`read promote` permissions. Ordinary `read write create` credentials cannot
register validation results, change the policy, or promote a revision.

## Configure required checks

Define an immutable environment identity, such as a container image digest plus
the dependency lock digest. The command and environment together determine the
configuration ID. A label such as `latest` weakens this identity when its contents
change; use a new identity for a changed environment or check.

```sh
seed-validate --url http://localhost:7331 --repo demo config \
  --environment image-and-lock-digest -- python -B -m unittest
```

Using an operator credential in `SEED_TOKEN`, configure the returned ID:

```sh
seed-validate --url http://localhost:7331 --repo demo policy CONFIGURATION_ID
```

A policy requires every configured check. Up to 16 configurations are supported.
Changing the policy invalidates prior verified/deployable pointers until those
exact revisions have passing results under the new policy. A subsequent failure
for the same revision/check invalidates its eligibility; retrying an older
successful HTTP request does not restore it.

## Asynchronous validation

After normal landing, a trusted worker runs a command on a pinned revision:

```sh
seed-validate --url http://localhost:7331 --repo demo run \
  --kind revision --subject REVISION_ID --workspace artifacts/check-01 \
  --environment image-and-lock-digest -- python -B -m unittest
seed-validate --url http://localhost:7331 --repo demo state
```

The workspace must be new. Its regular files come from the exact recorded
manifest and verified blob bytes. The worker uses the same local runner as
`seed-agent`, including timeout and child-process cleanup. Changed or deleted
versioned inputs make validation fail even if the command exits zero; new build
outputs can remain in the local workspace. The result records the exact tree,
command/environment configuration, exit status, elapsed time and a log digest.
The environment credential is removed from the command environment.

The worker runs with local privileges. External inputs need to be represented
by the environment identity and controlled by that worker. The current bounded
workspace supports up to 10,000 source files and the agent runner's changed-path
limit. This is a regular-file source snapshot, not a full OS image.

The operator can promote any revision that passes the current policy, including
a known good earlier revision for rollback:

```sh
seed-validate --url http://localhost:7331 --repo demo promote REVISION_ID
```

Promotion selects a deployment target; deployment remains the responsibility of
the consuming application or delivery pipeline. Read `validation-state` to obtain
the exact revision instead of assuming that the current head is deployable.

## Test a combined candidate before publishing

Strict publication is optional. Create a candidate from one or more pending
proposals, validate its combined state, and publish it:

```sh
seed-validate --url http://localhost:7331 --repo demo candidate PROPOSAL_1 PROPOSAL_2
seed-validate --url http://localhost:7331 --repo demo run \
  --kind candidate --subject CANDIDATE_ID --workspace artifacts/candidate-01 \
  --environment image-and-lock-digest -- python -B -m unittest
seed-validate --url http://localhost:7331 --repo demo publish CANDIDATE_ID
```

Candidate creation previews the same conditional landing logic in a rollback
savepoint. It changes neither the public head nor proposal status. Tests run
outside the publisher transaction. Publication rechecks the current policy and
requires the head to equal the tested candidate's starting head. A head advance
requires a new candidate and validation; the old result cannot validate another
combination. A published candidate can be retried idempotently after later heads
advance. Multiple proposals in one candidate publish atomically.

Workflow records use namespaced metadata entries and are included in logical
repository backups. They contain hashes and attestations rather than bearer
credentials or complete command transcripts.
