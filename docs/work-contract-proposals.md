# Recording follow-up proposals

This opt-in variant of the cooperative document workflow loads a supplied work
contract before either role starts. The caller chooses the objective, required
items and observations. The writer drafts a document. The reviewer chooses
complete, revise, investigate or defer, citing the observations and explaining
the decision. For any non-complete action it must describe a check or condition
that could change or resolve the decision. The host publishes the resulting
review as the terminal artifact; no dispatcher consumes the action.

Use the paired runtime revision 40a3baf4d9a195d565e6ecf9f1532b476c230425.
From the orchestrator root, after building runtime-workflow in role-runtime:

    ../role-runtime/bin/runtime-workflow --state /tmp/proposal-demo --run-id proposal-demo --backend codex --workers 1 --contract examples/discovery-contract.json

In another terminal, poll until done (a new Python process can resume each poll):

    PYTHONPATH=. .venv/bin/python scripts/check_model_workflow.py --runtime-state /tmp/proposal-demo --state artifacts/proposal-demo --run-id proposal-demo

Repeat with --verify to check the two completed tasks, export the receipt-bound
handoff, and show the recorded proposal. Persist the runtime/graph state together.
Reopening state binds the original contract. Use new state for changed inputs.

The example is a constructed mixed-interest travel interview. It is for checking
how the team reasons from limited evidence, not establishing actual travel
demand. Evidence IDs are checked for membership and uniqueness. Reference
integrity cannot establish whether an inference is warranted.

The hash-bound writer artifact includes the exact typed task_contract supplied
by the host. agent-eval receives that artifact and the recorded proposal through
the existing saved-output CLI. Optional semantic grading selects proposal-v1
for this artifact shape; it asks whether the reason and proposed decision
test follow from the evidence and objective. The decision is made by the
reviewer; the independent evaluator grades the recorded decision afterward.

    uv run --project evaluation/m1 --frozen python scripts/evaluate_handoff.py artifacts/proposal-demo/handoff-evidence --semantic-model codex-adapter/gpt-6.2-sol

Set AGENT_EVAL_CODEX_GRADER as in the existing evaluator runbook. Required-item
coverage for the review can still be requested with --requirements. Write a
contract for what the reviewer must explain if using this option.

Validation: an injected model received the contract in both roles, proposed
investigation and produced a completed review receipt without a downstream
message or wait. Contract tests exercised all four actions and rejected unknown
evidence, duplicate citations, changed document hashes, and missing next checks.
The affected Go packages passed vet/race checks. A separate agent-eval CLI
smoke test with a synthetic grader verified the proposal rubric, observations,
and artifact reached the grader; this measures wiring only.

Two attempts at live document generation ended at account_read / timeout /
not_started before the writer turn. A doctor check succeeded between them but
the last doctor check returned workspace_routing_timeout. No actual model
proposal or real proposal quality score was observed in these attempts.
