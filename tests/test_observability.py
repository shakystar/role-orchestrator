import threading
import unittest
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from role_orchestrator.observability import Observability, local_exporter


class Exporter(SpanExporter):
    def __init__(self, fail=False):
        self.spans = []
        self.fail = fail
    def export(self, spans):
        if self.fail:
            raise RuntimeError('controlled outage')
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS
    def shutdown(self):
        pass


class ObservabilityTests(unittest.TestCase):
    def test_parentage_and_metadata_filter(self):
        exporter = Exporter()
        observer = Observability(exporter)
        with observer.span('run', run_id='test', prompt='must-not-export') as parent:
            with observer.span('rpc.client', task_id='one'):
                pass
        result = observer.close()
        self.assertEqual(result['exported'], 2)
        child = next(s for s in exporter.spans if s.name == 'rpc.client')
        self.assertEqual(child.parent.span_id, parent.get_span_context().span_id)
        self.assertTrue(all('prompt' not in str(s.attributes) for s in exporter.spans))

    def test_export_failure_does_not_fail_work(self):
        observer = Observability(Exporter(fail=True))
        with observer.span('work'):
            answer = 42
        result = observer.close()
        self.assertEqual(answer, 42)
        self.assertEqual((result['failed'], result['exported']), (1, 0))

    def test_queue_bound_and_loss_accounting(self):
        entered, release = threading.Event(), threading.Event()
        class Blocked(Exporter):
            def export(self, spans):
                entered.set()
                if not release.wait(3):
                    return SpanExportResult.FAILURE
                return super().export(spans)
        observer = Observability(Blocked(), queue_size=8, batch_size=1)
        with observer.span('first'):
            pass
        self.assertTrue(entered.wait(1))
        try:
            for _ in range(30):
                with observer.span('work'):
                    pass
        finally:
            release.set()
        result = observer.close()
        self.assertEqual(result['ended'], 31)
        self.assertLessEqual(result['exported'], 9)
        self.assertGreaterEqual(result['unexported'], 22)

    def test_probe_rejects_remote_destination(self):
        with self.assertRaises(ValueError):
            local_exporter('https://cloud.langfuse.com', 'public', 'private')
