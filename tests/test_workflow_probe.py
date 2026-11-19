import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
BINARY=Path(os.environ.get('ROLE_WORKFLOW_PROBE_BIN',ROOT.parent/'role-runtime/bin/workflow-probe')).resolve()

@unittest.skipUnless(BINARY.is_file() or os.environ.get("ROLE_WORKFLOW_PROBE_BIN"),'build role-runtime/bin/workflow-probe for SMS feasibility tests')
class WorkflowProbeTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(BINARY.is_file(), "configured Go workflow fixture is missing")
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name)
        self.rpc('setup')
    def rpc(self,action):
        return json.loads(subprocess.check_output([str(BINARY),'--state',str(self.state/'runtime'),'--action',action],text=True))
    def poll(self,crash=False):
        args=[sys.executable,str(ROOT/'experiments/workflow_probe.py'),'--state',str(self.state),'--runtime',str(BINARY)]
        if crash:args+=['--crash-after-receipt']
        p=subprocess.run(args,capture_output=True,text=True)
        if crash:self.assertEqual(p.returncode,17,p.stderr);return
        self.assertEqual(p.returncode,0,p.stderr)
        return json.loads(p.stdout)
    def test_normal_restart_duplicate_and_unknown_usage(self):
        self.assertFalse(self.poll()['done'])
        self.rpc('complete');self.assertTrue(self.poll()['done'])
        for _ in range(2):
            self.rpc('duplicate');report=self.poll()
            self.assertEqual(report['unique_completed_tasks'],2)
            self.assertIsNone(report['usage']);self.assertIsNone(report['cost'])
        self.assertEqual(self.rpc('observe')['consumer_generation'],2)
    def test_early_and_wrong_correlation(self):
        self.poll();self.rpc('wrong');self.assertFalse(self.rpc('observe')['ready'])
        self.rpc('early');self.assertFalse(self.poll()['done'])
        self.assertEqual(self.rpc('observe')['producer'],'ready')
        self.rpc('complete');self.assertTrue(self.poll()['done'])
    def test_missing_and_corrupt_artifact(self):
        self.poll();self.rpc('complete')
        artifact=self.state/'runtime/artifact.json';data=artifact.read_bytes()
        artifact.unlink();self.assertFalse(self.poll()['done'])
        artifact.write_bytes(b'changed');self.assertFalse(self.poll()['done'])
        artifact.write_bytes(data);self.assertTrue(self.poll()['done'])
    def test_failed_producer_never_wakes_graph(self):
        self.poll();self.rpc('fail');self.rpc('early')
        self.assertFalse(self.poll()['done'])
        self.assertFalse(self.rpc('observe')['ready'])
    def test_receipt_before_checkpoint_crash(self):
        self.poll();self.rpc('complete');self.poll(crash=True)
        self.assertEqual(self.rpc('observe')['consumer'],'completed')
        self.assertTrue(self.poll()['done'])
        self.assertEqual(self.rpc('observe')['consumer_generation'],2)
        self.assertEqual(self.poll()['unique_completed_tasks'],2)
