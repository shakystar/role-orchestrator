# M2 handoff grounding probe, 2026-11-20

Tested agent-eval 89695e754a077cb8ed01db6cdd26ebd1d67826cf in the temporary
Python 3.12 evaluation environment, using its Go codex-grader and model
codex-adapter/gpt-6.2-sol. The operational M1 dependency remains pinned.

The observed review and the constructed control cite the same saved document.
Only the control's reason was changed: it attributes automatic retry of
uncertain work without reconciliation to a document that explicitly forbids it.
The declared expected outcomes were met and unmet respectively. These are
technical controls; no human adjudications were registered.

Both outputs passed the M1 source-hash criterion. M2 registered the two outputs,
task and grounding rubric. Evaluation grounding-live-01 recorded two dispatch
errors, both account_read / timeout / not_started (two initialization attempts
per item). There were zero semantic verdicts. A separate model-free doctor check
returned workspace_routing_timeout. Reliability reported unverified with zero
comparable pairs. This run cannot estimate semantic accuracy.

Private manifests, expected outcomes, reports and the inspectable store are in
artifacts/m2-check-89695e7/handoff-grounding. After a successful account
preflight, use the same store and frozen inputs with a fresh evaluation ID.
Preserve the failed evaluation for comparison.

## Retry after account recovery

The doctor check for the grader's /usr/local/bin/codex returned routingAvailable=true.
A fresh evaluation, grounding-live-02, used the same store, inputs, rubric and model.
Both items were graded without errors: the original review was met and the
contradictory reason was unmet, matching both predeclared controls. The two
unknown judgments in grounding-live-01 were retained. No firewall settings were
changed; these observations do not identify the cause of the earlier timeout.
The reliability report remains unverified with zero human-reference comparisons.
