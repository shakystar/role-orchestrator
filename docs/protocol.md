# Experimental bridge protocol v1

One UTF-8 JSON object per line on stdin; one response per line on stdout. Status metadata is written to stderr. Only one request is outstanding. The Go command is `runtime-bridge --state DIR --backend fixture|codex [--codex PATH]`. This is a local trusted interface, not a network service.

Request:

```json
{"id":"reviewer-0","role":"reviewer","instruction":"Review supplied evidence.","input":"{\"evidence\":[]}"}
```

Response:

```json
{"id":"reviewer-0","status":"succeeded","text":"...","cached":false,"stats":{"submitted":1,"succeeded":1,"failed":0,"rejected":0,"maxActive":1,"maxQueued":1,"workers":1}}
```

A cached response has `cached=true` and zero execution stats. An error has non-success `status` and `error`; Python stops that graph node. Malformed or oversized protocol frames terminate the bridge. Input lines are bounded at 128 KiB, task input at 64 KiB and executor text at 256 KiB. Role/project definitions are immutable. Task IDs are scoped to the state directory. Same ID with changed task contents or role instruction is rejected.

Go validates requests and executes through the existing `engine.Pool`, not directly from Python to the Codex adapter. The pilot borrows one bridge-owned executor across successive pool runs; each pool has one worker and queue capacity one. Shutdown closes the owned executor. A completed receipt is authoritative for execution deduplication. Existing non-success receipts fail closed. No automatic retry of ambiguous model calls.

LangGraph owns proposal/review/revision state. Go owns execution receipts and harness sessions. The graph may re-enter a node after interruption; its stable task ID and exact serialized input let the bridge reuse successful work. These two stores do not participate in a distributed transaction, and this pilot does not claim exactly-once side effects.

The `fixture` backend reads a synthetic response from the task input's `fixture` field. The live `codex` backend receives no such field. The immutable project definition binds backend selection, preventing a fixture state directory from becoming a live run accidentally.
