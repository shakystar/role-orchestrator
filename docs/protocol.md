# Bridge v1 contract and ownership

Status: implemented; this replaces the unversioned integration wire format. Both repositories contain identical request acceptance vectors (`contracts/v1-vectors.json` here; `cmd/runtime-bridge/testdata/v1-vectors.json` in Go). Python and Go validate independently; the integration suite tests the actual bridge.

## Compatibility

`runtime-bridge` requires `schema_version: 1` by default. Unversioned requests fail closed. `--allow-legacy` permits old four-field requests only in a legacy-bound state directory. Legacy and v1 states cannot be mixed. Existing unversioned receipts are not silently migrated; use a fresh v1 state directory. The default Python client always emits v1. Python's internal `execute({id, role, instruction, input})` call signature is unchanged; it constructs the wire envelope.

The current role definition version remains `v1` and capabilities remain `advisory`. Arbitrary role versions, capability negotiation, output-schema negotiation and dynamic task generation are outside this change.

## Request

```json
{"schema_version":1,"run_id":"run-1","attempt_id":"attempt-1","traceparent":"00-11111111111111111111111111111111-2222222222222222-03","id":"task-1","role":"reviewer","instruction":"Review supplied evidence.","input":"{\"evidence\":[]}"}
```

All fields are required; unknown and duplicate JSON keys are rejected. `schema_version` is integer 1 (boolean is invalid). Other values are strings. Identifiers match `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`. Instruction is 1..16384 UTF-8 bytes; input is 1..65536 UTF-8 bytes. The JSONL frame limit is 128 KiB; Python additionally caps encoded requests at 120 KiB. `traceparent` supports W3C version 00, nonzero lowercase trace/span IDs and a two-hex-digit flags byte, including Python OTel's random-trace flag `03`. Other traceparent versions and tracestate are not supported in this contract.

## Result

```json
{"schema_version":1,"run_id":"run-1","attempt_id":"attempt-1","id":"task-1","status":"succeeded","text":"...","cached":false,"stats":{"submitted":1,"succeeded":1,"failed":0,"rejected":0,"maxActive":1,"maxQueued":1,"workers":1}}
```

The envelope echoes run/task/attempt identity. `text` is a string, including when empty. `cached` is a boolean. Stats are nonnegative integers, not booleans. A successful noncached result records one submitted/successful task; a successful cached result has all-zero stats and creates no execution. Python rejects mismatched identity, unsupported versions, unknown fields and inconsistent status/cache accounting.

Errors have `status=error`, `cached=false`, `error_code` and a sanitized `error` string. Codes currently include `invalid_request`, `conflict`, `execution_blocked`, `execution_failed`. Detailed execution diagnostics remain local. A syntactically invalid request may not have identities to echo; Python does not submit invalid requests. A bad JSON frame gets an error response and does not start work; oversized scanner input terminates the bridge. There is no automatic retry. Business-result validation remains in the graph, separate from transport success.

## Identifier lifetime

| Identifier | Owner and lifetime |
| --- | --- |
| run_id | Python persists once in run-identity.json; Go binds its state directory to the run |
| trace_id | Persisted with run_id; reused across process resumes; Go rejects changing trace under the same run |
| task id | Logical immutable work item within a run |
| attempt_id | Python deterministically derives from run/task; Go durably binds it to one task before dispatch |
| span id | Fresh for each activation, delivery and actual execution; never reopened or reused |

Re-delivering the same completed task returns the original attempt and receipt. A different attempt ID for that task, or reuse of an attempt ID for a different task, is rejected. A genuine revised task must have a new task ID. Failed/uncertain work cannot be made retryable merely by changing its attempt ID. Automated recovery of such work is not implemented.

## Trace topology and restart

```text
orchestrator.activation (new root per process activation, same persisted trace_id)
 └─ orchestrator.rpc.client (new span per request)
     └─ runtime.request (Go server span, extracted W3C parent)
         └─ runtime.execute (only if actual executor runs)
```

A cache lookup produces client/request spans and a cache-hit event/attribute, but no `runtime.execute`. A resumed process creates a new activation root under the same trace; the prior span is not reopened. Multiple activation roots in one trace are deliberate. No artificial long-running root is kept alive across processes. Fixture executions are execution spans, not claimed model generations. Codex model-internal spans and token/cost accounting are outside scope.

Python and Go both write bounded metadata-only local span files (8 MiB cap each) and may independently batch-export to local Langfuse. Enable with CLI `--telemetry-env infra/langfuse/.env`. Run identity persists whether export is enabled or disabled. The Python client passes only explicit local telemetry configuration to Go through environment variables; keys are not passed as command-line arguments or written into manifests. Arbitrary remote exporters are not enabled by this pilot.

## State ownership and crash boundary

- Python: graph checkpoint, run identity, graph-level decisions.
- Go: immutable role/project definitions, protocol/run binding, task/attempt intent, execution receipt and harness session.
- OTel/Langfuse: observation only; never the authority for task completion or budget enforcement.

Go writes an immutable attempt intent before dispatch. If that intent exists without a receipt, re-delivery is blocked as uncertain. If a matching success receipt exists, it is reused even when the original graph checkpoint was not written. A failed/incomplete/corrupt receipt fails closed. The state directories are single-owner; the Go store lock protects bridge mutations. Python CLI also holds its owner lock. Direct Python Runtime use assumes one caller; it is not a concurrent client API.

The two stores do not share a transaction. This provides safe successful-result deduplication, not exactly-once external effects. No automatic legacy migration, uncertain-work recovery UI, distributed coordination, SMS graph wakeup or model retry was added.

## Telemetry failure policy

Both SDK queues are bounded to 64 spans with batches of 16. Go uses a two-second exporter timeout without exporter retries; Python uses its SDK's bounded HTTP timeout. Go shutdown has a three-second context; Python's local HTTP exporter has a two-second timeout. The generic Python facade cannot promise bounded shutdown for arbitrary third-party exporter implementations that ignore timeout. Export failures are summarized locally and do not change runtime receipts. Queue overflow, disk-write failure and failed exports can lose observations; no persistent replay or exactly-once telemetry is claimed. Local tracing failures must not start replacement work.
