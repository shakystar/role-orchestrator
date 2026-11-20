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

For post-run scoring, use Python 3.12 with the Inspect dependency pinned by
agent-eval M1. With M1 installed in that interpreter (or its frozen source on
PYTHONPATH), run from role-orchestrator:

    python scripts/evaluate_handoff.py path/to/graph-state/handoff-evidence

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
