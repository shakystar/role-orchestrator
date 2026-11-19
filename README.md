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

## CI access

These repositories are currently private. The default CI runs Python graph, contract and observability tests; cross-repository Go integration is opt-in because Actions tokens are repository-scoped. See [CI scope and access requirements](docs/validation.md#hosted-ci-scope). A skipped integration job does not count as a passing integration check; local integration and live-model results are recorded separately.

## Opportunity and planning stage

[기회 검토·후보 백로그](docs/planning/opportunities.md)에 관측 facade/Langfuse, 작업 계약, 제한된 후속 작업 제안, 컨텍스트 분리, 결과물 참조, 평가셋, 기억·스킬의 설계 후보를 등록했다. [관측 경계 초안](docs/planning/observability.md)과 [오픈소스 추출 근거](docs/planning/sources.md)를 함께 따른다. 문서/일부 소스 조사 결과이며 구현·정식 채택 완료를 뜻하지 않는다.

## Local observability feasibility probe

[Local Langfuse Docker runbook](infra/langfuse/README.md) and [validation record](docs/langfuse-validation.md): actual fixture graph/Go calls can be exported as Python client spans and read back from Langfuse v4. The v1 bridge now propagates W3C trace context into Go request/execution spans. Default CLI runs keep bounded metadata-only local span logs; add `--telemetry-env infra/langfuse/.env` for optional loopback Langfuse export. Real model costs and production observability remain out of scope. See [contract/trace validation](docs/contract-trace-validation.md).

Bridge v1 requires fresh state for old unversioned runs. Explicit legacy Go mode remains available in an isolated directory; no automatic receipt migration. See [protocol](docs/protocol.md) for identities, compatibility and recovery boundaries.

## Workflow feasibility

[SMS/checkpoint and measurement feasibility](docs/workflow-feasibility.md) records five synthetic restart, notification and artifact checks using the real Go store and LangGraph. This is an isolated experimental probe; the default CLI is still the fixed pilot. Production SMS wakeups and general metrics/usage aggregation remain to implement.

Next implementation: [workflow/metrics v1 preparation](docs/planning/workflow-metrics-v1.md) defines module ownership, local control RPC, wakeup/ack order, durable event projections and 19 acceptance specifications. These specifications are not executed test results.

## Owner-process workflow and metrics

[Implementation and runbook](docs/workflow-implementation.md): a separate workflow CLI now connects SMS, durable inbox, graph checkpoints and runtime receipts through the owning Go process. Independent metrics and a 19-case acceptance runner are included. The new host uses explicit synthetic tasks; production/model-backed rollout and token/cost accounting remain separate.
