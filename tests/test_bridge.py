import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from role_orchestrator.graph import build


class BridgeIntegrationTests(unittest.TestCase):
    def test_new_process_resume_and_completed_receipt_replay(self):
        binary = Path(os.environ.get('ROLE_RUNTIME_BIN', '../role-runtime/bin/runtime-bridge')).resolve()
        self.assertTrue(binary.is_file(), 'build the Go bridge first')
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            def cli(command):
                result = subprocess.run([sys.executable, '-m', 'role_orchestrator', command, '--state', directory, '--runtime', str(binary)], capture_output=True, text=True, check=True)
                return json.loads(result.stdout)
            first = cli('start')
            self.assertEqual((first['calls'], first['next']), (2, ['checkpoint']))
            # Simulate runtime completion followed by loss of the orchestration checkpoint:
            # ask the bridge for the exact completed task in a new process.
            receipts = list((state / 'runtime' / 'tasks').glob('*.json'))
            receipt = json.loads(receipts[0].read_text())
            task = receipt['task']
            from role_orchestrator.graph import INSTRUCTIONS
            request = {'id': task['id'], 'role': task['role'], 'instruction': INSTRUCTIONS[task['role']], 'input': task['input']}
            from role_orchestrator.runtime import Runtime
            client = Runtime(str(binary), state, 'fixture', 'codex')
            try:
                replay = client.execute(request)
                self.assertTrue(replay['cached'])
            finally:
                client.close()
            final = cli('resume')
            self.assertEqual((final['calls'], final['revisions'], final['next'], final['outcome']), (4, 1, [], 'accepted'))
            self.assertEqual(len(list((state / 'runtime' / 'tasks').glob('*.json'))), 4)
            self.assertEqual(cli('status')['calls'], 4)
            self.assertTrue(all(e['stats']['maxActive'] == 1 for e in final['events']))
            identity = json.loads((state/'run-identity.json').read_text())
            py_spans = [json.loads(l) for l in (state/'python-spans.jsonl').read_text().splitlines()]
            go_spans = [json.loads(l) for l in (state/'runtime/bridge-spans.jsonl').read_text().splitlines()]
            self.assertEqual(len([s for s in go_spans if s['name']=='runtime.execute']), 4)
            self.assertEqual(len([s for s in go_spans if s['name']=='runtime.request']), 5)
            self.assertTrue(all(s['trace_id']==identity['trace_id'] for s in py_spans+go_spans))
            clients = {s['span_id'] for s in py_spans if s['name']=='orchestrator.rpc.client'}
            self.assertTrue(all(s['parent_span_id'] in clients for s in go_spans if s['name']=='runtime.request'))
            cached = [s for s in go_spans if s['attributes'].get('cached')]
            self.assertEqual(len(cached), 1)
            self.assertEqual(len({s['span_id'] for s in py_spans+go_spans}), len(py_spans+go_spans))
            self.assertEqual(len([s for s in py_spans if s['name']=='orchestrator.activation']), 3)


if __name__ == '__main__':
    unittest.main()

class FailureBoundaryTests(unittest.TestCase):
    def test_export_failure_and_failed_task_replay(self):
        from role_orchestrator.runtime import Runtime
        from test_observability import Exporter
        binary = Path(os.environ.get('ROLE_RUNTIME_BIN', '../role-runtime/bin/runtime-bridge')).resolve()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            runtime = Runtime(str(binary), state, 'fixture', 'codex', exporter=Exporter(fail=True))
            good={'id':'ok','role':'r','instruction':'fixture','input':'{"fixture":{"ok":true}}'}
            bad={**good,'id':'bad','input':'{}'}
            try:
                self.assertEqual(runtime.execute(good)['status'],'succeeded')
                with self.assertRaisesRegex(RuntimeError,'execution_failed'):runtime.execute(bad)
                with self.assertRaisesRegex(RuntimeError,'execution_blocked'):runtime.execute(bad)
                self.assertTrue(runtime.execute(good)['cached'])
            finally:
                runtime.close()
            self.assertGreater(runtime.telemetry_result['failed'],0)
            spans=[json.loads(l) for l in (state/'runtime/bridge-spans.jsonl').read_text().splitlines()]
            executions=[s for s in spans if s['name']=='runtime.execute']
            self.assertEqual(len(executions),2)
            self.assertEqual(len([s for s in executions if s['status']=='Error']),1)
