import copy
import tempfile
import unittest
from pathlib import Path
from role_orchestrator.metrics import Metrics

def event(revision,kind='task.accepted',**payload):
    status={'attempt.started':'running','startup.retry':'running','result.cache_hit':'completed','task.waiting':'waiting'}.get(kind,'ready')
    data={'status':status,'history_complete':True,**payload}
    return {'schema_version':1,'event_id':f'e-{revision}','run_id':'test','task_id':'task','attempt_id':'attempt',
            'source':'runtime','revision':revision,'kind':kind,'occurred_at':'2026-11-19T00:00:00Z','payload':data}

def finished():
    return event(3,'attempt.finished',status='completed',outcome='completed',execution='completed',duration_ns=100,duration_complete=True)

class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'metrics.sqlite';self.m=Metrics(self.path,'test');self.addCleanup(self.m.close)
    def test_duplicate(self):
        page=[event(1),event(2,'attempt.started'),finished()]
        first=self.m.ingest(page,'x');second=self.m.ingest(page,'x')
        self.assertEqual(first,second);self.assertEqual(first['execution_attempts'],1);self.assertEqual(first['execution_duration_ns'],100)
    def test_conflict(self):
        self.m.ingest([event(1)])
        report=self.m.ingest([event(1,marker='changed')])
        self.assertIn('event_conflict',report['issues']);self.assertEqual(report['statuses']['task'],'ready')
    def test_reordered(self):
        report=self.m.ingest([finished()])
        self.assertEqual(report['completed_tasks'],1);self.assertIn('missing_attempt_start',report['issues'])
        report=self.m.ingest([event(1),event(2,'attempt.started')])
        self.assertEqual(report['completed_tasks'],1);self.assertNotIn('missing_attempt_start',report['issues'])
    def test_gap(self):
        self.assertIn('revision_gap',self.m.ingest([finished()])['issues'])
    def test_rebuild(self):
        page=[event(1),event(2,'attempt.started'),finished()];expected=self.m.ingest(page)
        rebuilt=Metrics(Path(self.tmp.name)/'new.sqlite','test')
        try:self.assertEqual(expected,rebuilt.ingest(list(reversed(page))))
        finally:rebuilt.close()
    def test_usage_missing(self):
        report=self.m.ingest([event(1)])
        self.assertIsNone(report['usage']);self.assertIsNone(report['cost']);self.assertTrue(report['usage_reason'])
    def test_transaction_rollback(self):
        def crash():raise RuntimeError('crash')
        with self.assertRaises(RuntimeError):self.m.ingest([event(1)],'next',crash)
        self.assertIsNone(self.m.cursor);self.assertEqual(self.m.summary()['logical_tasks'],0)
    def test_terminal_conflict_and_retry_cache(self):
        e=finished();other=event(4,'attempt.finished',status='failed',outcome='failed',execution='started',duration_ns=None)
        report=self.m.ingest([event(1),event(2,'attempt.started'),e,other,
            event(5,'startup.retry',retry_index=1),event(6,'result.cache_hit',delivery_id='delivery')])
        self.assertIn('terminal_conflict',report['issues']);self.assertEqual(report['completed_tasks'],0)
        self.assertEqual(report['startup_retries'],1);self.assertEqual(report['cache_deliveries'],1)
    def test_validation(self):
        for bad in [dict(event(1),schema_version=True),dict(event(1),run_id='other'),dict(event(1),revision=0)]:
            with self.assertRaises(ValueError):self.m.ingest([bad])
