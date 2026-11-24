import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from role_orchestrator import review_team


class TerminalOutcomeTest(unittest.TestCase):
    def test_missing_unavailable_reason_holds_without_resubmission(self):
        review = {
            'team': review_team.TEAM, 'task': 'terminal regression',
            'kind': 'criteria', 'stateful': False, 'authors': ['acl-007'],
            'evidence': {'synthetic': True},
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'state'
            request = {
                'version': 1, 'profile': review_team.PROFILE, 'input': review,
                'route': review_team.validate_request(review, 'codex'),
                'lab': temp, 'runtime': temp, 'revision': 'a' * 40,
                'backend': 'codex', 'parent': {},
            }
            outcome = root / 'outcome.json'
            with patch.object(review_team.subprocess, 'run',
                              side_effect=OSError('synthetic unavailable')) as submit:
                initial, code = review_team.run_review(root, request)
                self.assertEqual((initial['status'], initial['cause'], code),
                                 ('hold', 'member-unavailable', 3))
                self.assertEqual(initial['attempt'], {})
                submit.assert_called_once()
                saved = review_team.decode(outcome.read_bytes())
                self.assertEqual(saved, initial)
                del saved['reason']
                damaged = review_team.encode(saved) + b'\n'
                outcome.write_bytes(damaged)

                for readonly in (False, True):
                    with self.subTest(readonly=readonly):
                        result, code = review_team.run_review(root, request, readonly=readonly)
                        self.assertEqual((result['status'], result['cause'], code),
                                         ('hold', 'terminal-outcome', 3))
                        self.assertEqual(outcome.read_bytes(), damaged)
                        submit.assert_called_once()
