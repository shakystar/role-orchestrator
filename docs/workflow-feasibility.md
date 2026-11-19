# SMS workflow and measurement feasibility — 2026-11-19

Status: **synthetic feasibility passed; production integration not implemented**.
Priority is workflow wiring and measurable evaluation before content-level task contracts.
No model calls, live business research, external messages or new infrastructure were used.

Paired runtime fixture revision: `3169a9b7a6520144429db3ab015617c6e86cf5d1`. The optional private cross-repository CI job builds this fixture; the job still requires separately configured read access.

## Executed experiment

`experiments/workflow_probe.py` uses real LangGraph/SQLite checkpoints and invokes the
Go `experiments/workflowprobe` fixture in separate processes. The Go fixture uses existing
`engine.Store` cooperative receipts, leases, waiting transitions and mailbox/outbox
reconciliation. It does not copy these mechanisms into Python. The fixture is explicitly
synthetic and is not a general RPC API or a deployed listener.

Five Python integration tests passed in 19.136 seconds:

| Case | Observed result |
| --- | --- |
| Normal document handoff, process restart, repeated SMS | Graph completes; unique checkpoint completion count remains 2; consumer generation stays 2 (wait + consume) |
| Wrong correlation and SMS before producer completion | No graph resume until matching notification and completed producer receipt exist |
| Missing or modified artifact | Resume blocked; restored bytes matching receipt SHA-256 permit progress |
| Producer fenced as failed/uncertain, then SMS arrives | Graph remains waiting |
| Process exits after consumer receipt commit, before graph checkpoint | New Python process completes from cached receipt; no extra consumer lease/execution |

Every graph poll starts a separate Python process. The crash case uses `os._exit(17)`;
it is not just a mocked exception. Go checks authoritative producer completion and the
fixture artifact hash together with exact sender/recipient/correlation/message identity.
Repeated completion is a receipt lookup. The tests assert no new consumer generation.

Usage and cost remain null. Unique logical completions can be deduplicated in checkpoint
state; this test does **not** implement general attempt, timing, retry or token aggregation.
These five cases form a first executable regression set, not a model-quality evaluation.

## Findings that determine implementation

1. Existing mailbox matching alone marks a waiting runtime task ready. It does not prove
   the upstream task completed or its artifact exists. Graph wakeup must be gated by
   receipt and artifact checks. The fixture digest only checks byte identity, not truth.
2. Receipt persistence and graph checkpoints remain separate transactions. Stable task
   identity and receipt reuse handle the tested crash window; no exactly-once guarantee
   for arbitrary external effects follows.
3. Go state is single-owner. The probe closes the store between operations. Concurrent
   production orchestration needs a narrow API in the running runtime process for
   notifications, receipt lookup and acknowledgment/reconciliation. Python must not
   independently open live runtime storage or bypass its lock.
4. Runtime `Status` is a latest-state snapshot. Attempts, lifecycle history and measured
   phase durations cannot reliably be reconstructed from snapshots or wall-clock lease
   deadlines. Add durable bounded lifecycle records with stable event/run/task/attempt
   IDs before claiming attempt or latency aggregates.
5. Current adapter Usage summarizes successful turns only and can omit usage from
   failed turns. It is not a durable per-attempt usage ledger. A cost/usage feature needs
   per-attempt records, explicit completeness, model identity and versioned price basis.
   Missing usage is not zero; SDK/harness retries must not be counted as new logical tasks.
6. Observation/exporter state is not execution authority. Prior exporter-outage tests
   remain applicable; this probe does not depend on Langfuse and adds no durability claim.

## Next implementation slice

- Expose bounded mailbox/receipt query and notification delivery through the owning Go
  process; retain existing SMS identity rules and monotonic lease protections.
- Persist the graph's dependency/wakeup identity, recheck receipt and artifact on wake,
  acknowledge only after durable progress, and reconcile after restart. Duplicate,
  delayed and reordered notifications must converge to the same logical outcome.
- Add a durable lifecycle/usage ledger and deduplicating read-only aggregates. Separate
  task counts, execution attempts, cache deliveries and startup retries. Missing usage
  and unknown price remain explicitly unknown; estimates are not subscription invoices.
- Reuse these five fixtures, then add concurrent delivery, acknowledgment crash windows,
  reordered usage records and exporter outages to the automated evaluation runner.

Dynamic planning, richer content contracts, memory paging and business-quality evaluation
remain deferred. Technical feasibility supports starting this slice; it does not show
that full pipelining, complete cost accounting or autonomous research already works.

## Reproduce

```sh
# role-runtime
./scripts/go.sh build -o bin/workflow-probe ./experiments/workflowprobe
# role-orchestrator
ROLE_WORKFLOW_PROBE_BIN=../role-runtime/bin/workflow-probe \
  .venv/bin/python -m unittest discover -s tests -p test_workflow_probe.py -v
```

Tests use temporary state and do not alter existing runs. Without the fixture binary,
the optional test set is skipped; a skip is not a pass. The fixture assumes one owner
and a small local synthetic artifact, not concurrent callers or untrusted arbitrary paths.
