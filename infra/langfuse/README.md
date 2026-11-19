# Local Langfuse feasibility stack

Langfuse **4.82.0**, adapted from the [official Compose](https://github.com/langfuse/langfuse/blob/6dfeb6049b8fa2795af7bfce1cce9bbe60aca4c0/docker-compose.yml). Six official images are pinned by digest in compose.yaml and [images.lock.json](images.lock.json). Web, worker, PostgreSQL, Redis, ClickHouse and MinIO run in the dedicated `role-observability` Compose project. No AI gateway or model provider is configured.

## Start and access

From the role-orchestrator root:

```sh
python3 scripts/init_langfuse.py
# Existing .env is intentionally not overwritten; run init only once.
docker compose -f infra/langfuse/compose.yaml --env-file infra/langfuse/.env up -d
curl --max-time 10 http://localhost:3300/api/public/health
```

Wait for health to return `{"status":"OK","version":"4.82.0"}` before probing. Initial database migrations and cold startup can take time. `up -d` alone does not prove web readiness.

Open **http://localhost:3300**. Login email is `local@role-runtime.test`; the generated password is `LANGFUSE_INIT_USER_PASSWORD` in the local `infra/langfuse/.env`. The project is `Orchestrator Synthetic Validation`. The `.env` also holds API credentials; it is mode 0600 and ignored by Git. Do not paste it into logs or issues. The script uses Langfuse's official initialization variables to create the local user/project.

Only web `127.0.0.1:3300` and MinIO `127.0.0.1:3900` are published. Other database/queue ports stay on the Compose network. There are no host directory mounts, privileged containers, disabled certificate checks, alternate registries, or firewall/proxy changes. Telemetry is disabled in Langfuse. Docker's own network configuration was not changed.

Docker Desktop in the tested environment has 6 CPUs and roughly 8 GiB VM memory. This is an environment observation, not a minimum-resource recommendation. Data persists in named volumes. Container log rotation is limited to three 10 MB files per service; database and application data are not automatically size-limited by this pilot.

## Probe

Build the existing Go bridge and install locked Python dependencies using the root README. Then:

```sh
uv run --frozen python scripts/probe_langfuse.py \
  --runtime ../role-runtime/bin/runtime-bridge \
  --state artifacts/langfuse-new
```

Each run requires a new state directory. The probe executes the real LangGraph graph and real Go pool with deterministic fixture responses, without model calls. OTel sends one root span plus four **Python-side RPC client spans** to `/api/public/otel/v1/traces`. Auth uses the local project key pair in memory. It queries `/api/public/v2/observations?traceId=...` and verifies five observations, parent IDs, completion times and absent input/output payloads.

**Langfuse v4 events_only deployments disable legacy `/api/public/traces` reads.** A 404 there does not mean OTLP ingestion failed. The probe uses the supported v2 observation API. See the [official API definition](https://github.com/langfuse/langfuse/blob/6dfeb6049b8fa2795af7bfce1cce9bbe60aca4c0/fern/apis/server/definition/observations.yml).

## Stop, resume and outage check

```sh
# Stop services; preserve all stored data.
docker compose -f infra/langfuse/compose.yaml --env-file infra/langfuse/.env stop
# Resume existing services.
docker compose -f infra/langfuse/compose.yaml --env-file infra/langfuse/.env start
# Inspect status.
docker compose -f infra/langfuse/compose.yaml --env-file infra/langfuse/.env ps
```

For a controlled failure test, stop **only web**, run the probe with a fresh `--state` and `--expect-unavailable`, then start web in a `finally`/cleanup path. The probe must still finish four fixture tasks, report failed exports and make no model calls. Failed telemetry is not replayed by this prototype; a successful flush means the exporter finished processing, **not that data was delivered**.

If installation encounters firewall, proxy, TLS or policy errors, stop and report the error. Do not disable security checks, change firewall settings, use alternate image mirrors or force installation. This is an explicit user constraint.

## Boundaries

The OTel facade is a small, optional probe module: bounded queue (64 spans), batches of 16, bounded attributes, two-second HTTP exporter timeout, failure/loss counters. Tests use in-memory exporters, including controlled failure and blocked transport. No disk spool or guaranteed delivery is implemented.

This probe does not instrument Go internal spans, propagate W3C context over JSONL, connect separate Python-process trace sessions, estimate token cost, or verify model usage. No tokens or prices are fabricated for fixture work. Runtime ownership/receipts are unchanged; observation failures do not drive task retries or workflow completion.
