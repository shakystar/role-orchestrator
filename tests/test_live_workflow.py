"""Synthetic tasks in a live owning runtime process; no model calls."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from role_orchestrator.workflow.client import Client, ControlError
from role_orchestrator.metrics import Metrics

ROOT=Path(__file__).resolve().parents[1]
BINARY=Path(os.environ.get('ROLE_WORKFLOW_BIN',ROOT.parent/'role-runtime/bin/runtime-workflow')).resolve()

@unittest.skipUnless(BINARY.is_file() or os.environ.get('ROLE_WORKFLOW_BIN'),'build runtime-workflow first')
class LiveWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(BINARY.is_file())
        self.root=Path(tempfile.mkdtemp(prefix='rw-',dir='/tmp'))
        self.addCleanup(shutil.rmtree,self.root)
        self.server=None;self.log=None;self.addCleanup(self.stop)
        self.sender={'project':'pipeline','role':'producer','version':'v1','instance':'main'}
        self.recipient={**self.sender,'role':'consumer'}
        config={'dependency':{'sender':self.sender,'recipient':self.recipient,'correlation':'document-v1','producer_task_id':'producer','artifact_version':'v1'},
                'consumer_task':{'id':'consumer',**self.recipient,'input':'synthetic'}}
        (self.root/'config.json').write_text(json.dumps(config))
    def start(self,mode='normal',delay='100ms'):
        self.log=(self.root/'server.log').open('a')
        self.server=subprocess.Popen([str(BINARY),'--state',str(self.root/'runtime'),'--socket',str(self.root/'runtime/control/runtime.sock'),'--run-id','test','--fixture-mode',mode,'--fixture-delay',delay],stdout=subprocess.PIPE,stderr=self.log,text=True)
        line=self.server.stdout.readline()
        if not line:self.fail('runtime failed: '+(self.root/'server.log').read_text())
        self.assertEqual(json.loads(line)['backend'],'fixture')
        self.client=Client(self.root/'runtime/control/runtime.sock','test')
    def stop(self):
        if self.server:
            if self.server.poll() is None:
                self.server.terminate()
                try:self.server.wait(timeout=10)
                except subprocess.TimeoutExpired:self.server.kill();self.server.wait();self.fail('runtime did not stop')
            self.server.stdout.close();self.server=None
        if self.log:self.log.close();self.log=None
    def poll(self,crash=None,may_wait=False):
        args=[sys.executable,'-m','role_orchestrator.workflow','--state',str(self.root/'graph'),'--socket',str(self.root/'runtime/control/runtime.sock'),'--run-id','test','--config',str(self.root/'config.json')]
        if crash:args+=['--fixture-crash',crash]
        p=subprocess.run(args,cwd=ROOT,text=True,capture_output=True,timeout=15)
        if crash and p.returncode==17:return {'crashed':True}
        if crash and not may_wait:self.fail('crash boundary not reached: '+p.stderr)
        self.assertEqual(p.returncode,0,p.stderr);return json.loads(p.stdout)
    def until_crash(self,point):
        for _ in range(15):
            report=self.poll(point,may_wait=True)
            if report.get('crashed'):return
            time.sleep(.05)
        self.fail('crash boundary not reached')
    def complete(self):
        for _ in range(15):
            report=self.poll()
            if report['done']:return report
            time.sleep(.05)
        self.fail('workflow did not complete')
    def events(self):
        cursor=None;rows=[]
        while True:
            page=self.client.call('events.list',after_cursor=cursor,limit=100);rows+=page['items']
            if page['next_cursor']==cursor or len(page['items'])<100:return [r['data'] for r in rows]
            cursor=page['next_cursor']
    def test_sms_normal(self):
        self.start();self.assertTrue(self.complete()['done'])
        self.assertEqual(self.client.call('receipts.get',task_id='consumer')['generation'],1)
    def test_sms_duplicate(self):
        self.start();self.complete()
        for _ in range(2):self.assertTrue(self.poll()['done'])
        self.assertEqual(self.client.call('receipts.get',task_id='consumer')['generation'],1)
        self.assertEqual(sum(e['kind']=='attempt.started' and e['task_id']=='consumer' for e in self.events()),1)
    def test_sms_early(self):
        self.start('early','2s');self.assertFalse(self.poll()['done']);self.assertFalse(self.poll()['done']);self.complete()
    def test_sms_scope(self):
        self.start('wrong');self.complete()
        with self.assertRaises(ControlError):self.client.call('notifications.list',consumer_id='foreign',after_cursor=None,limit=100)
        other=Client(self.root/'runtime/control/runtime.sock','other')
        with self.assertRaises(ControlError):other.call('health.get')
    def test_artifact_invalid(self):
        self.start('missing');self.poll();report=self.poll();self.assertFalse(report['done']);self.assertIn('artifact_invalid',report['blocked_reasons'])
    def test_producer_uncertain(self):
        self.start('failed');self.poll();self.assertFalse(self.poll()['done'])
        self.assertEqual(self.client.call('receipts.get',task_id='producer')['status'],'uncertain')
    def test_checkpoint_crash(self):
        self.start();self.until_crash('receipt_before_checkpoint');self.assertTrue(self.complete()['done'])
        self.assertEqual(self.client.call('receipts.get',task_id='consumer')['generation'],1)
    def test_ack_crash(self):
        self.start();self.until_crash('checkpoint_before_ack')
        self.assertEqual(list((self.root/'runtime/control/acks').glob('*.json')),[])
        self.assertTrue(self.poll()['done'])
        self.assertEqual(self.client.call('receipts.get',task_id='consumer')['generation'],1)
    def test_inbox_crash(self):
        self.start();time.sleep(.2);self.poll('inbox_before_cursor');self.complete()
    def test_active_query(self):
        self.start(delay='3s');began=time.monotonic();report=self.client.call('health.get')
        self.assertTrue(report['enabled']);self.assertLess(time.monotonic()-began,1)
        self.assertNotEqual(self.client.call('receipts.get',task_id='producer')['status'],'completed')
    def test_restart_and_rebuild(self):
        self.start();self.complete();events=self.events();m=Metrics(self.root/'metrics.sqlite','test')
        try:expected=m.ingest(events)
        finally:m.close()
        self.stop();self.start();self.assertTrue(self.poll()['done']);m=Metrics(self.root/'rebuilt.sqlite','test')
        try:self.assertEqual(expected,m.ingest(self.events()))
        finally:m.close()
    def test_langfuse_down(self):
        # This path has no dependency on an exporter. A failing exporter is exercised
        # independently by test_bridge.FailureBoundaryTests in the same evaluation run.
        self.start();self.complete();m=Metrics(self.root/'metrics.sqlite','test')
        try:
            result=m.ingest(self.events());self.assertEqual(result['completed_tasks'],2)
        finally:m.close()
