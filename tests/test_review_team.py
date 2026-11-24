import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERDICT_FILE = "assignment-v2.verdict.json"
MEMBERS = {
    "acl-010": ("Criteria Reviewer", "criteria-review"),
    "acl-011": ("Counterexample Reviewer", "counterexample-review"),
    "acl-012": ("Code Reviewer", "code-review"),
    "acl-013": ("Recovery Reviewer", "recovery-review"),
    "acl-014": ("Reproduction Reviewer", "reproduction-review"),
    "acl-015": ("Provenance Reviewer", "provenance-review"),
    "acl-005": ("Oracle Reviewer", "oracle-reviewer-v2"),
}
MEMBER_FOR_ROLE = {role: member for member, (_, role) in MEMBERS.items()}
FINAL = "acl-005"


def objects(value):
    if isinstance(value, dict):
        yield value
        for key, child in value.items():
            if key != "fixtures":  # Request fixtures are inputs, not results.
                yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def persisted_attempts(state):
    """Return reviewer -> actual attempt ID from persisted verdicts only."""
    attempts = {}
    for path in sorted(state.rglob(VERDICT_FILE)):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{path}: invalid verdict JSON") from exc
        if not isinstance(record, dict) or not {
            "actual_member_id", "run_id", "attempt_id", "artifact"
        } <= record.keys():
            raise AssertionError(f"{path}: missing persisted verdict fields")
        member, run, attempt = (record[key] for key in ("actual_member_id", "run_id", "attempt_id"))
        if not isinstance(member, str) or not member.strip() or not all(
            type(value) is int or (isinstance(value, str) and value.strip())
            for value in (run, attempt)
        ):
            raise AssertionError(f"{path}: invalid persisted verdict IDs")
        artifact = record["artifact"]
        if not isinstance(artifact, dict) or (
            artifact.get("member_id") != member
            or type(artifact.get("attempt_id")) is not type(attempt)
            or artifact["attempt_id"] != attempt
        ):
            raise AssertionError(f"{path}: verdict artifact identity mismatch")
        payload = artifact.get("payload")
        if not isinstance(payload, dict) or not {"decision", "reason", "owner"} <= payload.keys():
            raise AssertionError(f"{path}: missing verdict payload fields")
        if member in attempts:
            raise AssertionError(f"{path}: duplicate persisted verdict for {member}")
        attempts[member] = attempt
    return attempts


def has_review_output(state, decision, reason):
    for path in state.rglob("*.json"):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if any(
            record.get("decision") == decision and record.get("reason") == reason
            for record in objects(document)
        ):
            return True
    return False


def markdown_table(headers, rows):
    return "\n".join(
        "| " + " | ".join(row) + " |"
        for row in (headers, ("---",) * len(headers), *rows)
    ) + "\n"


class PersistedAttemptTests(unittest.TestCase):
    def test_reads_attempt_id_from_nested_verdict_not_path_or_other_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            output = state / "arbitrary-layout" / "not-the-attempt-id"
            output.mkdir(parents=True)
            (output / VERDICT_FILE).write_text(
                json.dumps({"actual_member_id": "acl-010", "run_id": "test-run", "attempt_id": "actual-id", "artifact": {"member_id": "acl-010", "attempt_id": "actual-id", "payload": {"decision": "allow", "reason": "synthetic test fixture", "owner": "test-owner"}}}),
                encoding="utf-8",
            )
            (state / "other.json").write_text(
                json.dumps({"member_id": "acl-010", "attempt_id": "unrelated-id"}),
                encoding="utf-8",
            )
            self.assertEqual(persisted_attempts(state), {"acl-010": "actual-id"})


class ReviewTeamCLITests(unittest.TestCase):
    def setUp(self):
        binary = os.environ.get("ROLE_RUNTIME_BIN")
        if not binary:
            self.skipTest("Set ROLE_RUNTIME_BIN to the real Go role runtime fixture")
        self.runtime = Path(binary).expanduser().resolve()
        self.assertTrue(
            self.runtime.is_file() and os.access(self.runtime, os.X_OK),
            f"ROLE_RUNTIME_BIN is not an executable file: {self.runtime}",
        )
        tmp = tempfile.TemporaryDirectory(prefix="review-team-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lab = self.root / "lab"
        roster = self.lab / "docs/team/roster.md"
        handoffs = self.lab / "docs/features/development-handoffs.md"
        for path in (roster, handoffs):
            path.parent.mkdir(parents=True)
        roster.write_text(
            "# Team roster\n\n" + markdown_table(
                ("member_id", "name", "duty", "role"),
                [(member, name, "independent-review", role)
                 for member, (name, role) in MEMBERS.items()],
            ),
            encoding="utf-8",
        )
        handoffs.write_text(
            "# Development handoffs\n\n" + markdown_table(
                ("role / title", "inputs", "outputs", "Sol / high"),
                [(f"{role} / {title}", "evidence", "review-verdict.json", "Sol / high")
                 for title, role in MEMBERS.values()],
            ),
            encoding="utf-8",
        )
        self.git("init", "-q")
        self.git("add", "--", "docs")
        self.git("commit", "-qm", "Commit review team fixture")
        self.revision = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "user.name=Review Team Test", "-c",
             "user.email=review-team@example.invalid", "-c", "commit.gpgsign=false", *args],
            cwd=self.lab, check=True, capture_output=True, text=True, timeout=30,
        ).stdout

    def request(self, kind, *, stateful=False):
        return {
            "team": "independent-review",
            "task": "Review the synthetic evidence",
            "kind": kind,
            "stateful": stateful,
            "authors": ["acl-007"],
            "evidence": {"sample": "x"},
            "fixtures": {
                member: {"decision": "accept", "reason": f"Fixture accepts for {member}",
                         "owner": "acl-007"}
                for member in MEMBERS
            },
        }

    def invoke(self, state, request):
        state.mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            [sys.executable, "-m", "role_orchestrator.review_team",
             "--lab", str(self.lab), "--revision", self.revision,
             "--runtime", str(self.runtime), "--state", str(state),
             "--backend", "fixture"],
            cwd=PROJECT_ROOT, input=json.dumps(request) + "\n", text=True,
            capture_output=True, timeout=90, check=False,
        )

    @staticmethod
    def diagnostics(result):
        return f"exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    def run_accepted(self, state, request, expected_members):
        result = self.invoke(state, request)
        self.assertEqual(result.returncode, 0, self.diagnostics(result))
        attempts = persisted_attempts(state)
        self.assertEqual(set(attempts), expected_members, self.diagnostics(result))
        return attempts

    def test_minimum_routing(self):
        for kind, specialists in (
            ("criteria", {"acl-010"}),
            ("counterexample", {"acl-011"}),
            ("patch", {"acl-012", "acl-014", "acl-015"}),
            ("reproduction", {"acl-014"}),
            ("provenance", {"acl-015"}),
        ):
            with self.subTest(kind=kind):
                self.run_accepted(self.root / kind, self.request(kind), specialists | {FINAL})

    def test_persisted_attempts_rejects_malformed_evidence(self):
        state = self.root / "malformed-verdict-state"
        state.mkdir()
        path = state / VERDICT_FILE
        artifact = {
            "member_id": "acl-010",
            "attempt_id": "attempt-1",
            "payload": {"decision": "revise", "reason": "Missing evidence"},
        }
        receipt = {
            "actual_member_id": "acl-010",
            "run_id": "run-1",
            "attempt_id": "attempt-1",
            "artifact": artifact,
        }
        for label, record, error in (
            ("bare fixture input", artifact, "missing persisted verdict fields"),
            ("feedback without owner", receipt, "missing verdict payload fields"),
            ("mismatched attempt", {**receipt, "attempt_id": "attempt-2"}, "identity mismatch"),
        ):
            with self.subTest(case=label):
                path.write_text(json.dumps(record), encoding="utf-8")
                with self.assertRaisesRegex(AssertionError, error):
                    persisted_attempts(state)

    def test_stateful_patch_routes_to_all_required_reviewers(self):
        self.run_accepted(
            self.root / "state", self.request("patch", stateful=True),
            {"acl-012", "acl-013", "acl-014", "acl-015", FINAL},
        )

    def test_replay_preserves_actual_attempt_ids(self):
        state = self.root / "state"
        request = self.request("patch", stateful=True)
        expected = {"acl-012", "acl-013", "acl-014", "acl-015", FINAL}
        first = self.run_accepted(state, request, expected)
        replay = self.run_accepted(state, request, expected)
        self.assertEqual(replay, first)

    def test_changed_evidence_is_rejected_under_same_state(self):
        state = self.root / "state"
        original = self.request("criteria")
        first = self.run_accepted(state, original, {"acl-010", FINAL})
        changed = copy.deepcopy(original)
        changed["evidence"]["sample"] = "y"
        result = self.invoke(state, changed)
        self.assertGreater(result.returncode, 0, self.diagnostics(result))
        self.assertEqual(persisted_attempts(state), first)

    def test_revise_stops_before_final_review(self):
        state = self.root / "state"
        request = self.request("criteria")
        reason = "The synthetic sample needs a clearer acceptance criterion"
        request["fixtures"]["acl-010"] = {
            "decision": "revise", "reason": reason, "owner": "acl-007",
        }
        result = self.invoke(state, request)
        self.assertEqual(set(persisted_attempts(state)), {"acl-010"}, self.diagnostics(result))
        self.assertTrue(has_review_output(state, "revise", reason), self.diagnostics(result))

    def test_self_review_is_rejected_before_dispatch(self):
        # A conflict on the final reviewer must be caught before even the first
        # reviewer runs, not merely when the final review would be dispatched.
        for author in ("acl-010", FINAL):
            with self.subTest(author=author):
                state = self.root / author
                request = self.request("criteria")
                request["authors"].append(author)
                result = self.invoke(state, request)
                self.assertGreater(result.returncode, 0, self.diagnostics(result))
                self.assertEqual(persisted_attempts(state), {})
                artifacts = [p.relative_to(state) for p in state.rglob("assignment-v2*")
                             if p.is_file()]
                self.assertEqual(artifacts, [], self.diagnostics(result))


if __name__ == "__main__":
    unittest.main()
