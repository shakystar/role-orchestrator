# role-orchestrator

Experimental Python LangGraph control plane for [role-runtime](https://github.com/shakystar/role-runtime).

**A minimal integration pilot, not an adopted production orchestrator.** LangGraph owns the investigation graph and checkpoints; Go owns bounded execution, role sessions and durable task receipts; codex-adapter owns the Codex protocol. No Python model provider or API key is needed: the live pilot uses the locally authenticated Codex harness through Go.

```text
LangGraph + SQLite → JSONL → role-runtime Pool (one slot) → codex-adapter → Codex
```

The deliberately unsupported initial claim is reviewed against two **synthetic** cases. The reviewer can accept or request revision. One revision is allowed, followed by acceptance or deferral. A deliberate interrupt after the first review tests restart in a separate process. This is a persistence test, not a business approval gate.

## Run

Python 3.11+, uv, and a checkout of role-runtime containing `cmd/runtime-bridge`:

```sh
cd ../role-runtime
./scripts/go.sh build -o bin/runtime-bridge ./cmd/runtime-bridge
cd ../role-orchestrator
uv sync --frozen
uv run --frozen python -m role_orchestrator start --state artifacts/fixture-01 --runtime ../role-runtime/bin/runtime-bridge
uv run --frozen python -m role_orchestrator resume --state artifacts/fixture-01 --runtime ../role-runtime/bin/runtime-bridge
uv run --frozen python -m role_orchestrator status --state artifacts/fixture-01 --runtime ../role-runtime/bin/runtime-bridge
uv run --frozen python -m unittest discover -s tests -v
```

The fixture backend uses explicitly supplied deterministic responses **inside the real Go pool**. It tests wiring, not model reasoning. For live model execution, add `--backend codex --codex /absolute/path/to/codex` to all commands and use a fresh state path. Codex authentication and network access are required. On macOS, optionally prefix the execution commands with `caffeinate -i` to prevent idle system sleep for that command only.

A state directory owns one experiment and fixed configuration. `start` refuses an existing manifest. `resume` continues an interrupted or failed graph, but the bridge refuses to replay runtime work with an uncertain or failed receipt. Such work needs explicit recovery; this pilot does not implement a recovery UI. `status` does not start Go or Codex. Keep graph and runtime state together. Stored model output and session data are local and ignored by Git.

## Scope and boundaries

- Three roles, one live execution slot, at most four logical tasks and one revision.
- Sequential JSONL transport, one bridge process per CLI invocation. Within it the same Codex process is reused across roles.
- SQLite checkpoints survive Python process restart. Successful Go receipts are reused for the exact same task and immutable role definition.
- Uncertain external work is **not exactly-once**: a crash after model completion but before durable receipt cannot be inferred safe to replay.
- No dynamic team creation, web collection, business demand validation, SMS-driven graph wakeups, distributed scheduling, general cancellation API, memory paging or production decision policy.
- No fixed model-turn deadline. Adapter status/heartbeat goes to the local `runtime-status.jsonl`; the pilot client waits synchronously. This is not a production health supervisor.
- Local single-user macOS/Linux experiment (`fcntl` lock); Windows support is not implemented.
- Returned evidence IDs are checked for membership, not truth or entailment. A model can still make a poor decision.

See [protocol](docs/protocol.md), [validation](docs/validation.md), and [dependencies](THIRD_PARTY.md). Formal adoption and detailed product planning remain a separate decision after this pilot.
