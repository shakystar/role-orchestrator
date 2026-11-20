import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from role_orchestrator.workflow.handoff import export_handoff


class SnapshotTests(unittest.TestCase):
    def test_repeated_capture_and_conflict(self):
        with TemporaryDirectory() as directory:
            state = Path(directory)
            artifacts = state / 'artifacts'
            artifacts.mkdir()
            receipts = {}
            for task, name in [('producer', 'document.json'), ('consumer', 'review.json')]:
                data = task.encode()
                (artifacts / name).write_bytes(data)
                receipts[task] = dict(task={'id': task}, attempt_id=task+'-attempt', status='completed',
                                      artifact_valid=True, artifact={'id': name, 'sha256': hashlib.sha256(data).hexdigest()})
            snapshot = export_handoff(state, 'run', artifacts, receipts)
            self.assertEqual(export_handoff(state, 'run', artifacts, receipts), snapshot)
            original = (snapshot / 'manifest.json').read_bytes()
            receipts['consumer']['attempt_id'] = 'different-attempt'
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                export_handoff(state, 'run', artifacts, receipts)
            self.assertEqual((snapshot / 'manifest.json').read_bytes(), original)

    def test_invalid_receipt_bytes_never_publish(self):
        with TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'document.json').write_bytes(b'changed')
            producer = dict(task={'id':'producer'}, status='completed', artifact_valid=True,
                            artifact={'id':'document.json', 'sha256':'0'*64})
            with self.assertRaisesRegex(ValueError, 'changed since receipt'):
                export_handoff(state, 'run', state, {'producer':producer})
            self.assertFalse((state / 'handoff-evidence').exists())
