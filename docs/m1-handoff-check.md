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
