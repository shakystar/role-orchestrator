# Dependencies and reuse

Versions are locked in `uv.lock`; install with `uv sync --frozen`.

| Component | Locked direct version | License | Boundary / alternative |
| --- | --- | --- | --- |
| [LangGraph](https://github.com/langchain-ai/langgraph) | 1.2.22 | MIT | StateGraph, conditional edges, interrupt/resume. Alternative: small explicit state machine or Go Eino; not benchmarked here. |
| [SQLite checkpointer](https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-sqlite) | 3.1.3 | MIT | Local durable graph checkpoints. Alternative: other LangGraph checkpoint backends. |
| [role-runtime](https://github.com/shakystar/role-runtime) | See validation runtime revision | Repository terms | External executable, no Go implementation copied into Python. |
| [codex-adapter](https://github.com/shakystar/codex-adapter) | Pinned by role-runtime | MIT | Used transitively by Go for Codex app-server. |

LangGraph is used as an installed library without a fork. SQLite is part of Python's standard library; the checkpointer additionally installs its own dependencies. Transitive packages are recorded in the lockfile; LangSmith hosted tracing is not required or configured by this project. Installed dependency metadata was inspected for the direct Python packages. Codex itself is not redistributed.

## Optional local observability probe

OpenTelemetry Python SDK and OTLP/HTTP exporter **1.45.2**, Apache-2.0 (installed package metadata checked), provide batching and transport; no fork. Alternative: Langfuse SDK; not compared here. Docker Compose is adapted from Langfuse source commit `6dfeb6049b8fa2795af7bfce1cce9bbe60aca4c0`; images and digests are recorded under infra/langfuse. Langfuse and supporting services run as separate official containers; their code is not copied into this repository. Upstream component licenses apply: consult [Langfuse](https://github.com/langfuse/langfuse/blob/main/LICENSE), [PostgreSQL](https://www.postgresql.org/about/licence/), [Redis](https://github.com/redis/redis/blob/7.4/COPYING), [ClickHouse](https://github.com/ClickHouse/ClickHouse/blob/master/LICENSE), and [MinIO](https://github.com/minio/minio/blob/master/LICENSE). These images are not all governed by the Python project's MIT license; Redis 7.4 has separate licensing terms. This pilot uses no enterprise features or hosted model gateway.

The bridge v1 client uses the same Python OTel versions. The paired Go bridge now pins OTel 1.47.0 (Apache-2.0); see role-runtime THIRD_PARTY.md and go.sum. The shared request-vector files are project-authored, not copied from upstream.
