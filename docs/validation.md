# Integration validation — 2026-11-19

Status: **minimal integration passed; formal adoption not decided**.

Runtime revision: [`94bf2e798fc53736babb1b6974f5a84cd7cee00b`](https://github.com/shakystar/role-runtime/commit/94bf2e798fc53736babb1b6974f5a84cd7cee00b). LangGraph 1.2.22, SQLite checkpointer 3.1.3, Python 3.11.9, Go 1.26.11, Codex 0.169.1 on macOS. Python dependencies are pinned in uv.lock; CI pins the Go revision. See [machine-readable metrics](validation-metrics.json).

## Executed checks

| Check | Result |
| --- | --- |
| Python → actual Go bridge → actual engine.Pool, fixture backend | Passed |
| LangGraph checkpoint after first review; resume in separate Python/Go processes | Passed, fixture and live Codex |
| Conditional acceptance without revision | Passed, controlled response test |
| Continued disagreement stops after one revision | Passed: deferred, four tasks |
| Exhausted two-task budget blocks further dispatch | Passed |
| Matching completed Go receipt reused after restart | Passed, fixture and actual model output |
| Replay without a usable Codex executable | Passed; cached response, no new model/status events |
| Conflicting task/role, uncertain receipt, corrupted receipt | Rejected by Go tests |
| Go fmt/vet and whole-repository race tests | Passed before lazy-start refinement; targeted bridge vet/race tests passed after refinement |
| Python unittest suite | Four tests passed after final bridge change |

## Live execution

Two live experiments completed, each with synthetic evidence only. Final `codex-02` used the final runtime revision. `start` executed proposer and reviewer and stopped at a persisted interrupt. `resume`, launched separately, executed reviser and reviewer, then ended. Four durable successful receipts and four completed adapter status events were independently checked. Each CLI invocation reused one Codex process for its two tasks: two distinct Codex process IDs across the full experiment, with one active worker at a time.

The initial unsupported claim was that lodging-area selection is travelers' primary unmet need. The reviewer requested revision because the supplied synthetic cases did not establish that conclusion. The revised proposal preserved uncertainty and was accepted on re-review. The live backend received no fixture response or forced reviewer action. However, the initial claim, evidence and role instructions were deliberately designed for this integration check: **this does not establish broad autonomous research quality or real tourism demand**.

Each task was shorter than the configured 15-second heartbeat interval in the final run. Status events were observed; this run does not newly validate long-turn heartbeat recovery. No web search, recruitment, paid campaign, third-party contact, or product deployment occurred.

## Failure found and corrected

The first live graph (`codex-01`) completed all four tasks. A subsequent cached-receipt check in the restricted environment failed with `codex RPC stdout closed`: the bridge eagerly initialized Codex before checking receipts. Backend initialization is now lazy. A Go regression test prevents backend initialization on cache hits; final actual model output was successfully read with `/nonexistent/codex` as the executable. The failed check is preserved as a failure, not counted as a successful replay. The full live graph was repeated as `codex-02` after the change.

## Limits and next decision

This pilot uses three roles, one worker, one permitted revision, a local SQLite checkpoint, and a sequential bridge. It does not validate general dynamic planning, concurrent scheduling, SMS-driven graph resumption, crash recovery of ambiguous external effects, cost estimation, research methodology, or memory paging. The Go task receipt and Python checkpoint are separate stores; no exactly-once side-effect guarantee is made.

Formal planning should begin only if this boundary is accepted. The next design questions would be versioned task/event contracts, SMS-to-graph wakeups, evidence/decision records and budget policy. They are intentionally not implemented in this experiment.

## Hosted CI scope

The initial [CI run](https://github.com/shakystar/role-orchestrator/actions/runs/44457505973) failed while checking out role-runtime, before any tests ran. Both repositories are private; the default Actions token for role-orchestrator cannot read role-runtime. Repository visibility and account credentials were not changed.

CI now always runs the three Python graph tests. The real Go bridge integration job is explicitly opt-in: configure a narrowly scoped `ROLE_RUNTIME_READ_TOKEN` secret with read access to role-runtime and set `ROLE_RUNTIME_INTEGRATION=true` as a repository variable. Until then, that job is skipped, **not passed**. All four tests including the real bridge were run locally. No hosted live Codex execution is configured.
