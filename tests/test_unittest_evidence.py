from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from role_orchestrator.unittest_evidence import matched_unittest_runs


SUITE = """
import unittest

class GoalTest(unittest.TestCase):
    def test_status(self):
        {body}
"""


class UnittestEvidenceTests(unittest.TestCase):
    def test_compare_real_runner_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def run(label, suite, *options):
                (root/'test_goal.py').write_text(suite)
                result = subprocess.run(
                    [sys.executable, '-B', '-m', 'unittest', *options, 'test_goal'],
                    cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                log = root/(label + '.log')
                log.write_bytes(result.stdout)
                return log

            baseline = run('red', SUITE.format(body='self.assertEqual(1, 2)'), '-v')
            candidate = run('green', SUITE.format(body='self.assertEqual(2, 2)'), '-v')
            self.assertTrue(matched_unittest_runs(baseline, candidate))
            for label, suite in [('empty', 'import unittest'),
                                 ('skipped', SUITE.format(body='self.skipTest("no result")')),
                                 ('different', SUITE.replace('test_status', 'test_other').format(body='pass')),
                                 ('more', SUITE.format(body='pass') + '\n    def test_other(self): pass')]:
                with self.subTest(label=label):
                    self.assertFalse(matched_unittest_runs(baseline, run(label, suite, '-v')))
            self.assertFalse(matched_unittest_runs(run('error', 'import missing_goal_module', '-v'), candidate))
            self.assertFalse(matched_unittest_runs(candidate, candidate))
            subtests = 'for value in (1, 2):\n            with self.subTest(value=value):\n                '
            sub_red = run('red-subtest', SUITE.format(body=subtests + 'self.assertEqual(value, 0)'), '-v')
            sub_green = run('green-subtest', SUITE.format(body=subtests + 'self.assertGreater(value, 0)'), '-v')
            self.assertTrue(matched_unittest_runs(sub_red, sub_green))
            baseline = run('red-quiet', SUITE.format(body='self.fail("missing result")'), '-q')
            candidate = run('green-quiet', SUITE.format(body='pass'), '-q')
            self.assertTrue(matched_unittest_runs(baseline, candidate))
            baseline.write_bytes(baseline.read_bytes().replace(b'AssertionError:', b'RuntimeError:'))
            self.assertFalse(matched_unittest_runs(baseline, candidate))
