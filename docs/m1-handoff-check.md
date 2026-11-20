# Saved review hash calibration (2026-11-20)

The producer document and consumer review from model-1119a were recovered from
their saved model outputs after the temporary runtime directory was removed.
Both reconstructed files matched the pre-existing restart baseline SHA-256.
The baseline sample carries the completed consumer attempt ID from the local
metrics ledger. The second sample is an offline control with one changed hex
digit in source_sha256 and its own synthetic ID.

The public agent-eval M1 CLI at 16b96ee28b58a92c463d157b421ed2c13becedf9
imported both outputs via saved-output-v1. Inspect AI (ec5d110b) evaluated
/source_sha256 against the hash of the producer file using its mock provider.
Initial and parent-linked repeat evaluations both reported baseline met and
control unmet, with two judgeable samples and zero scorer errors. A second
import returned duplicate. No runtime or Codex turn was launched.

Reproduce in a Python 3.12 environment containing M1's pinned Inspect dependency.
Use an immutable M1 source checkout, the saved evidence directory (document.json,
review.json, restart-baseline.json, metrics.sqlite), and a fresh output directory:

    python scripts/check_m1_handoff.py M1_SOURCE SAVED_EVIDENCE NEW_WORK_DIR

The work directory contains private inputs, a local store and both JSON/Markdown
reports. This checks referential integrity only. The two calibration samples
represent one model review and one offline mutation.

## Operational handoff to M1

The model workflow check with `--verify` captures both artifacts under the
graph state's `handoff-evidence/` alongside their receipt hashes and attempt IDs.
Publication uses a directory rename. Identical snapshots may be reused. Changed
bytes or attempt identities cannot overwrite the snapshot. The verification JSON
now returns `handoff_evidence` in place of `review_matches_document`. Go continues
to validate prerequisites at execution time. Graph completion and model tracing
checks still run without Inspect installed.

From the role-orchestrator root, use the dedicated Python 3.12 uv project.
The first invocation syncs the evaluation environment and runs the scorer:

    uv run --project evaluation/m1 --frozen python scripts/evaluate_handoff.py path/to/graph-state/handoff-evidence

To install separately, run `uv sync --project evaluation/m1 --frozen`. Initial
setup needs Git access to the pinned repositories and package index access.
The separate evaluation lock pins agent-eval with the Inspect extra and its
transitive dependencies. Update that lock deliberately when adopting a new
evaluator revision.

Validated against agent-eval commit 16b96ee28b58a92c463d157b421ed2c13becedf9.
Only the public CLI and version 1 manifests are used. Each invocation creates
a separate private store and report in `handoff-evaluations/<evaluation_id>/`
alongside the snapshot. Its result.json binds run, consumer attempt, evaluation,
criterion, output digest and report. A completed report supplies the draft outcome.
An unmet rule exits nonzero with a result; a CLI or evidence error raises an error
without publishing a result. Runtime completion receipts stay authoritative for
execution status. Tokens and cost are not inferred. Use the original calibration
probe above for parent-linked regrading in a single store.

Migration check: the archived model-1119a bytes and completed attempt from its
metrics ledger were used to reconstruct the receipt fields for the export test.
The original yielded met and a one-character mutation yielded unmet, matching
the previous comparison. Neither command launched model work. This test exercised
snapshot export and real M1 CLI scoring offline, not another live workflow.

Git installation check (2026-11-20, Intel macOS, CPython 3.12.13, uv 0.11.13):
a new evaluation virtualenv installed 81 packages with frozen sync. Installed
VCS metadata matched the agent-eval and Inspect commits recorded in the lock.
With PYTHONPATH unset, frozen offline uv runs over the retained original and
mutated snapshots produced met (exit 0) and unmet (exit 1), respectively.

## Optional semantic grading

The operational evaluation lock now uses M2 commit
89695e754a077cb8ed01db6cdd26ebd1d67826cf. The existing evaluation/m1 project
path and default hash-only command remain compatible. For a live semantic
review, build the M2 codex-grader from that pinned agent-eval checkout
(`cd adapters/codex && go build -o /absolute/path/to/codex-grader .`).
With local Codex authentication, run from role-orchestrator:

    export AGENT_EVAL_CODEX_GRADER=/absolute/path/to/codex-grader
    uv run --project evaluation/m1 --frozen python scripts/evaluate_handoff.py path/to/handoff-evidence --semantic-model codex-adapter/gpt-6.2-sol

Enabling the flag sends the document, rubric and saved review to the selected
grader. result.json retains the hash outcome and adds a semantic object with
its evaluation ID, outcome, processing status, structured failure when available,
and the M2 reliability report path when produced. A graded unknown indicates
insufficient evidence; scorer_error indicates an evaluation failure. For a
completed command, exit 0 means all requested checks met, 1 means an unmet
check, and 2 means at least one unknown. No downstream work is submitted.

End-to-end validation with this command: the archived original scored met/met
(exit 0); the same hash plus a reversed reason scored met/unmet (exit 1). An
injected pre-dispatch account timeout scored met/unknown with the not_started
failure preserved (exit 2). The mutated hash scored unmet in default mode.

## Independent reviewer preparation

Use evaluation/grounding/blind-cases.json and rubric-v1.txt together. Copy
human-review.csv into private storage, enter reviewer, met/unmet/unknown, the
relevant source phrase (or identify missing evidence), and reasoning for each
case. Freeze the completed sheet before viewing any model responses or doing
model comparison. The five synthetic cases are ungraded preparation material.
No human reference or agreement statistic is claimed. When comparing later,
bind the frozen labels to the exact case, source and rubric versions.
