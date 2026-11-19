# Local Langfuse validation — 2026-11-19

Scope: technical feasibility of a local Docker receiver, bounded metadata-only OTel export and independent REST readback. Not business utility, full pipeline E2E, or a complete production observability facade.

## Environment and actual checks

- Langfuse 4.82.0, six official images pinned by digest; Docker Engine 20.10.20 / Compose 2.12.1 on macOS.
- Local web: http://localhost:3300; only loopback web/MinIO ports published. Docker VM: 6 CPUs, ~8 GiB RAM.
- Python OTel SDK and OTLP/HTTP exporter 1.45.2; installed versions locked in uv.lock.
- Eight local unittest tests passed: existing graph/Go bridge tests plus parentage/metadata filtering, exporter exception isolation, bounded queue loss accounting, remote endpoint rejection.
- `langfuse-probe-03`: four actual Go fixture tasks completed, zero model calls, five spans exported, five observations independently retrieved from v2 observation API. Same-trace parentage, end times and absent input/output payloads verified.

- Controlled outage (`langfuse-outage-01`): stopped only web; four fixture tasks completed, five spans reported export failure, no model calls. Web was restarted in a cleanup path.
- Recovery (`langfuse-recovery-01`): health OK, login page HTTP 200, four new fixture tasks completed and all five spans read back with valid parentage and no input/output payloads. All six services remain running for local use.
- Compose configuration validation and local documentation links passed. Cloud CI tests the graph and exporter units only; Docker integration was executed locally.

## Failures retained

1. Docker initially answered its API but image pulls failed with `desktop-containerd/containerd.sock: connection refused`. No containers were running. Docker Desktop restart was attempted; the normal quit request stalled, so relevant Docker app/VM processes received TERM before restart. No data reset, firewall/proxy changes, TLS changes or alternate mirrors were used. After startup completed, official image downloads and service health checks succeeded. The later user constraint against bypassing network policy is also documented in the runbook.
2. `langfuse-probe-01`: web cold startup was not ready; health timed out and five spans failed export. The four fixture tasks still completed. This is a failed ingestion check, not a successful trace.
3. `langfuse-probe-02`: five spans were accepted, but the initial validation used legacy `/api/public/traces` and returned 404. Response explicitly identified v4 events_only mode. Official API definitions were checked; the probe now uses `/api/public/v2/observations`. Deployment mode was not weakened to enable legacy APIs.

## What is not validated

Python client RPC spans describe round-trip duration, not Go scheduler/model latency. Go-internal OTel propagation, cross-process trace continuation, real model usage/cost, cache telemetry deduplication, persistent export retry and sustained resource overhead are still pending. The graph's existing same-process checkpoint continuation is used in this probe; the previous separate-process graph-resume experiment remains a different result.

LangGraph/Go receipts remain authoritative for execution. Langfuse is an asynchronous observational sink. SDK queue overflow can lose telemetry; counters expose this in tests after draining. No exactly-once telemetry or durable spool guarantee is made. Model costs are unknown/not applicable for this fixture experiment, not measured as a monetary zero.
