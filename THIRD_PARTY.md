# Dependencies and reuse

Versions are locked in `uv.lock`; install with `uv sync --frozen`.

| Component | Locked direct version | License | Boundary / alternative |
| --- | --- | --- | --- |
| [LangGraph](https://github.com/langchain-ai/langgraph) | 1.2.22 | MIT | StateGraph, conditional edges, interrupt/resume. Alternative: small explicit state machine or Go Eino; not benchmarked here. |
| [SQLite checkpointer](https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-sqlite) | 3.1.3 | MIT | Local durable graph checkpoints. Alternative: other LangGraph checkpoint backends. |
| [role-runtime](https://github.com/shakystar/role-runtime) | See validation runtime revision | Repository terms | External executable, no Go implementation copied into Python. |
| [codex-adapter](https://github.com/shakystar/codex-adapter) | Pinned by role-runtime | MIT | Used transitively by Go for Codex app-server. |

LangGraph is used as an installed library without a fork. SQLite is part of Python's standard library; the checkpointer additionally installs its own dependencies. Transitive packages are recorded in the lockfile; LangSmith hosted tracing is not required or configured by this project. Installed dependency metadata was inspected for the direct Python packages. Codex itself is not redistributed.
