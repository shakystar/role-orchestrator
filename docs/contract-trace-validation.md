# Bridge v1 / trace propagation acceptance — 2026-11-19

Status: implementation and synthetic acceptance passed. **Successful live model acceptance remains pending** because the account preflight returned `workspace_routing_timeout`. No model turn started in that probe. The goal is not complete; this is not formal orchestrator adoption or a business efficacy evaluation.

## Implemented boundary

- Strict versioned request/result envelopes and independent Python/Go validators with identical request vectors.
- Durable run/trace identity, immutable task/attempt binding, isolated explicit legacy mode.
- W3C parent propagation across Python subprocess transport into Go request and execution spans.
- Resume reuses the trace with fresh span IDs; cache replay creates no execution span.
- Failed/uncertain receipts are not automatically retried. Observation failure cannot authorize replacement work.
- Optional loopback Langfuse export; bounded metadata-only local logs and SDK queues.

Paired runtime revision: `47eb03531097e925145682600d30cb96c1ac6f8e` (also pinned by the optional hosted integration job).

## Evidence

| Check | Result |
| --- | --- |
| Python unittest discovery against built Go binary | 12 tests passed, including subprocess resume/cache and exporter failure |
| Go vet + race suite | Passed; targeted bridge checks repeated after the initialization cleanup fix |
| Shared wire vectors | Python and Go agree, including version/type/duplicate/unknown/case-sensitive keys and W3C flags `03` |
| Fixture graph start → separate process resume → cache replay | Four actual fixture executions, five deliveries, 17 stored observations; every ID and parent matched local records |
| Trace payload exclusion | No input/output payload on the 17 fixture observations |
| Unavailable loopback exporter | Task succeeded; both Python and Go recorded two failed exports; completion including cleanup 1.495 s, zero model calls |
| Live attempt | Failed at account preflight (`workspace_routing_timeout`); zero model turns started |
| Live failure trace readback | All four observations stored; exact parents, ERROR status and no input/output payload verified |
| Model-free official doctor | Independently returned the same routing error; no evidence establishes rate limiting or local path misrouting |

The live failure also exposed a pre-existing lazy initialization bug: a typed-nil adapter was stored after its constructor returned an error, then panicked on close. The bridge now stores an executor only after successful construction. A regression test returns a typed nil plus error and verifies safe close. This fix does not claim to repair upstream routing.

The initial Go OTel 1.38.0 rejected Python OTel 1.45.2's valid W3C flags `03`. Go OTel was upgraded to 1.47.0 and the case added to shared regression vectors. A dependency fetch initially failed under sandbox DNS restrictions; standard-source download subsequently succeeded with normal execution approval. No firewall, proxy, certificate or mirror settings were changed.

## Reproduce

```sh
# Build ../role-runtime/bin/runtime-bridge first.
ROLE_RUNTIME_BIN=../role-runtime/bin/runtime-bridge .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/validate_contract_trace.py \
  --state artifacts/contract-trace-fixture-new \
  --runtime ../role-runtime/bin/runtime-bridge
```

The local Langfuse runbook supplies the ignored environment file. Add `--live --codex /absolute/path/to/codex` for one model task and a cache replay after routing is available. Always use a fresh state for a new experiment; never delete failed receipts to force a replay. The live acceptance script has no model retry loop. It polls only the observation API for ingestion.

Sanitized measurements are in `contract-trace-metrics.json`. Prompts, receipts, session identifiers, status logs and credentials stay in ignored local artifacts. The fixture and live-failure records are separate; a failed model preflight is not counted as a successful live call.

## Remaining gate and limits

One successful real model task followed by cached replay with an unusable Codex path, plus complete Langfuse parent readback, remains required to finish this goal. Hosted private cross-repository integration stays opt-in; a skipped CI integration job is not a pass. No production deployment, general scheduler, dynamic work generation, SMS integration, model cost accounting or telemetry durability guarantee was added.
