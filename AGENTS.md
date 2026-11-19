# Scope

- This is a minimal integration experiment. Read README.md and docs/validation.md before changes.
- Do not expand into production orchestration or business research without a task requiring it.
- Keep decision/checkpoint state in Python and execution/session receipts in role-runtime.
- Do not call Codex directly from Python or copy Go runtime internals here.
- Preserve explicit fixture vs live-model evidence. Never report synthetic evidence as market validation.
- Bound retries, graph loops and work submission. Never silently replay uncertain external work.
- Keep artifacts, model transcripts, credentials and local sessions out of Git.
- Pin dependencies with uv.lock. Run tests against the real Go bridge for protocol changes.

# User-facing completion reports

- Follow [completion reporting v1](docs/completion-reporting.md).
- Lead with concrete before/after behavior and what the user can now do. Keep the implemented components visible immediately afterward; both are required.
- Distinguish implementation, integration, synthetic verification and real-model/operational verification. Do not inflate the baseline or claim unmeasured improvements.
- End with concise verification, remaining scope and supporting links. Scale the format to the task; do not replace the explanation with file lists or test counts.
