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


class ControlledRuntime:
    def __init__(self, action):
        self.action = action
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        value = json.loads(request['input'])['fixture']
        if request['role'] == 'reviewer':
            value['action'] = self.action
        return {'id': request['id'], 'status': 'succeeded', 'text': json.dumps(value), 'cached': False, 'stats': {}}


class GraphTests(unittest.TestCase):
    def run_graph(self, action, budget=4):
        runtime = ControlledRuntime(action)
        with SqliteSaver.from_conn_string(':memory:') as saver:
            graph = build(runtime, saver, 'fixture', budget)
            config = {'configurable': {'thread_id': 'test'}}
            graph.invoke({'proposal': {}, 'review': {}, 'revisions': 0, 'calls': 0, 'events': [], 'outcome': ''}, config)
            return graph.invoke(Command(resume=True), config), runtime.calls

    def test_accept_skips_revision(self):
        state, count = self.run_graph('accept')
        self.assertEqual((count, state['revisions'], state['outcome']), (2, 0, 'accepted'))

    def test_unresolved_review_stops_after_one_revision(self):
        state, count = self.run_graph('revise')
        self.assertEqual((count, state['revisions'], state['outcome']), (4, 1, 'deferred'))

    def test_budget_blocks_dispatch(self):
        runtime = ControlledRuntime('revise')
        with SqliteSaver.from_conn_string(':memory:') as saver:
            graph = build(runtime, saver, 'fixture', budget=2)
            config = {'configurable': {'thread_id': 'budget'}}
            graph.invoke({'proposal': {}, 'review': {}, 'revisions': 0, 'calls': 0, 'events': [], 'outcome': ''}, config)
            with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
                graph.invoke(Command(resume=True), config)
            self.assertEqual(runtime.calls, 2)


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
            result = subprocess.run([str(binary), '--state', str(state / 'runtime'), '--backend', 'fixture'], input=json.dumps(request)+'\n', text=True, capture_output=True, check=True)
            self.assertTrue(json.loads(result.stdout)['cached'])
            final = cli('resume')
            self.assertEqual((final['calls'], final['revisions'], final['next'], final['outcome']), (4, 1, [], 'accepted'))
            self.assertEqual(len(list((state / 'runtime' / 'tasks').glob('*.json'))), 4)
            self.assertEqual(cli('status')['calls'], 4)
            self.assertTrue(all(e['stats']['maxActive'] == 1 for e in final['events']))


if __name__ == '__main__':
    unittest.main()
