# SMS workflow and metrics v1 implementation

2026-11-19. The first implementation connects owner-process runtime control, durable
notification inboxes, LangGraph wakeup/ack, runtime lifecycle records, independent
metrics and a fixed acceptance runner. It uses synthetic tasks; no model calls or
business-quality evaluation. The existing default CLI remains the earlier fixed pilot.

Paired runtime source: `19e41795ab4ea63005f60c00b92c126e25a79d4f`, pinned by the optional hosted integration job.

## Delivered boundaries

| Module | Implemented behavior |
| --- | --- |
| Go control | Private Unix socket in the runtime owner, four-connection limit, bounded pages, scope checks, receipt/artifact query, ack and immutable downstream submission |
| Go lifecycle | Atomic cooperative receipt + event intent, replayable durable projection, capacity reservation, stable event IDs, legacy completeness marker |
| workflow.client | Bounded JSON RPC, run/request correlation, explicit errors; no model or task retry loop |
| workflow.runner | SQLite inbox/cursor transaction, binding and owner lock, receipt/artifact gate, LangGraph restart, checkpoint-before-ack ordering |
| metrics.store | No LangGraph/Langfuse dependency; SQLite raw events, cursor and derived projection in one transaction, duplicate/conflict handling, rebuildable aggregates |
| evaluation | Maps all 19 specified case IDs to executable Python/Go tests; records revisions, dirty flags, timing, expected result, checks and pass/fail/skip; any failure/skip exits nonzero |

`tasks.submit` was added to the prepared control contract: a resumed graph needs a
bounded path to submit its downstream task to the existing owner. Only configured
consumer addresses are accepted; changed task content conflicts and failed task IDs
are not requeued. Completed duplicate submission creates a cache-delivery observation,
not another execution. The original JSONL bridge v1 stays unchanged.

Metrics include unique logical tasks, execution attempts, terminal statuses, startup
retry indexes, cache deliveries, measured runtime execution and queue/suspended segment
durations. Missing/restarted intervals are explicitly incomplete. `complete` describes
consistency of **observed events**, not proof that every possible source task was fetched.
`coverage=observed_events_only` makes that limit explicit. Graph dependency-wait duration,
per-attempt tokens and monetary cost remain null with reasons; no fabricated zero costs.

Notifications and source events remain in Go; Python inbox and metrics DBs are derived.
A runtime cursor is not an event timestamp or causal ordering. Consumers transactionally
retain early/unresolved notifications while advancing the read cursor. Completion uses
runtime receipts and an artifact SHA-256 check, never Langfuse arrival or SMS alone.
The runner is a bounded poll command, not a background daemon or a distributed scheduler.

## Reproduce the two-task pipeline

Build and run the Go host in terminal 1:

```sh
cd ../role-runtime
./scripts/go.sh build -o bin/runtime-workflow ./cmd/runtime-workflow
./bin/runtime-workflow --state /tmp/role-flow-demo --run-id demo --fixture-delay 2s
```

From role-orchestrator in terminal 2, invoke the poll command until `done=true`.
Each invocation may be a new process; both inbox and graph checkpoints persist:

```sh
.venv/bin/python -m role_orchestrator.workflow \
  --state artifacts/workflow-demo --run-id demo \
  --socket /tmp/role-flow-demo/control/runtime.sock \
  --config examples/document-workflow.json
```

Fetch bounded event pages and materialize metrics:

```sh
.venv/bin/python -m role_orchestrator.metrics \
  --state artifacts/workflow-demo/metrics.sqlite --run-id demo \
  --socket /tmp/role-flow-demo/control/runtime.sock
```

The CLI reports whether it reached an unchanged cursor within its page budget.
Rebuild by fetching the retained source into a **new** metrics DB. No source receipts
are deleted or retried. Use fresh state paths for new runs; preserve uncertain records.

## Acceptance runner

```sh
# Build runtime-bridge too: the exporter-failure regression uses that real bridge.
.venv/bin/python -m role_orchestrator.evaluation \
  --runtime-source ../role-runtime --output artifacts/workflow-evaluation.json
```

The 19 case specifications remain in `contracts/workflow-metrics-cases.json`.
The first complete development run passed 19, failed 0, skipped 0. Its raw report is
ignored under artifacts; a final source-bound report is recorded separately after
verification. Projection recovery injects a filesystem failure then reopens the store;
graph checkpoint/inbox/ack crash tests actually terminate the Python subprocess.
Langfuse isolation combines the independent no-export workflow path with the existing
failing-exporter/real-bridge regression; it does not claim a live Langfuse outage test.

Unix socket tests require local socket permissions. The initial sandbox denied bind;
normal local execution permission allowed the same tests, without firewall/proxy/TLS
changes. No new dependency, Docker image, service, repository or hosted resource was added.

## Remaining limits

- `runtime-workflow` is a fixture host; reusable control/lifecycle packages are not yet
  wired into every existing model-backed entry point. Legacy bridge receipts are not
  backfilled into the lifecycle ledger.
- Per-attempt startup retry recording is an engine hook. Backend construction outside
  an admitted task is not falsely assigned to an attempt. New model-backed wiring and
  real token usage events require a later integration test.
- File ledgers are bounded and retained; retention, archival, distributed readers and
  production-grade indexed storage are future work. Logical capacity reservation does
  not prevent actual disk I/O failure.
- One owner per graph/runtime; no exactly-once guarantee for arbitrary external effects.
- Content-level task contracts, dynamic teams and DB memory paging remain deferred.
