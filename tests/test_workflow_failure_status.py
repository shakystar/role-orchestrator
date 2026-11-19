import tempfile
import unittest
from role_orchestrator.workflow.client import ControlError
from role_orchestrator.workflow.runner import Workflow


class FailedProducer:
    run_id = 'failed-producer'

    def call(self, method, **params):
        if method == 'notifications.list':
            return {'items': [], 'next_cursor': None}
        if method == 'receipts.get':
            if params['task_id'] == 'producer':
                return {'status': 'failed', 'failure': {'execution': 'not_started', 'code': 'workspace_routing_timeout'}}
            raise ControlError('not_found')
        raise AssertionError('failure must not submit downstream work')


class FailureStatusTests(unittest.TestCase):
    def test_failed_producer_visible_without_sms(self):
        with tempfile.TemporaryDirectory() as root:
            workflow = Workflow(root, FailedProducer(),
                                {'producer_task_id': 'producer', 'correlation': 'document-v1'},
                                {'id': 'consumer'})
            try:
                for _ in range(2):
                    report = workflow.poll()
                    self.assertFalse(report['done'])
                    self.assertEqual(report['task_statuses'], {'producer': 'failed', 'consumer': 'missing'})
                    self.assertEqual(report['failures']['producer']['execution'], 'not_started')
            finally:
                workflow.close()
