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


if __name__ == '__main__':
    unittest.main()
