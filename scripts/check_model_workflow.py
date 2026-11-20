"""One bounded observation/poll of an already running Go model workflow.

Never launches Codex, retries a failed task, or imposes a model-turn deadline.
Run with PYTHONPATH=. from the repository root. Raw artifacts stay local.
"""
import argparse
import json
from pathlib import Path

from role_orchestrator.workflow.handoff import export_handoff
from role_orchestrator.metrics import Metrics
from role_orchestrator.workflow.client import Client
from role_orchestrator.workflow.runner import Workflow


def check(runtime, state, run_id, config, verify=False):
    client = Client(runtime / 'control/runtime.sock', run_id)
    workflow = Workflow(state, client, config['dependency'], config['consumer_task'])
    try:
        report = workflow.poll()
    finally:
        workflow.close()
    metrics = Metrics(state / 'metrics.sqlite', run_id)
    try:
        for _ in range(10):
            cursor = metrics.cursor
            metrics.sync(client)
            if metrics.cursor == cursor:
                break
        else:
            raise RuntimeError('event page budget exceeded; observe again')
        report['metrics'] = metrics.summary()
    finally:
        metrics.close()
    if verify:
        assert report['done'], 'workflow not complete'
        receipts = {}
        for task in ('producer', 'consumer'):
            receipt = client.call('receipts.get', task_id=task)
            assert receipt['status'] == 'completed' and receipt['artifact_valid']
            assert receipt['execution'] == 'completed'
            receipts[task] = receipt
        statuses = [json.loads(s) for s in (runtime / 'model-status.jsonl').read_text().splitlines()]
        completed = [s for s in statuses if s['phase'] == 'completed']
        assert len(completed) == 2, 'model completion count changed'
        spans = [json.loads(s) for s in (runtime / 'workflow-spans.jsonl').read_text().splitlines()]
        assert len(spans) == 4, 'wait for asynchronous trace flush, or investigate replay'
        assert len({s['span_id'] for s in spans}) == 4
        assert all(s['status'] == 'Ok' for s in spans)
        trace_id = json.loads((runtime / 'workflow-trace-id.json').read_text())
        assert {s['trace_id'] for s in spans} == {trace_id}
        parents = {s['span_id'] for s in spans if s['name'] == 'workflow.step'}
        assert len(parents) == 2
        assert all(s['parent_span_id'] in parents for s in spans if s['name'] == 'workflow.model')
        m = report['metrics']
        assert (m['logical_tasks'], m['execution_attempts'], m['completed_tasks']) == (2, 2, 2)
        assert m['complete'] and m['failed_tasks'] == 0
        assert m['usage'] is None and m['cost'] is None
        evidence = export_handoff(state, run_id, runtime / 'artifacts', receipts)
        review = json.loads((evidence / 'review.json').read_text())
        report['verification'] = {'model_completions': len(completed), 'span_count': len(spans),
                                  'trace_id': trace_id, 'handoff_evidence': str(evidence)}
        if 'action' in review:
            report['verification']['proposal'] = review
        else:
            report['verification']['review_verdict'] = review['verdict']
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-state', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--config', type=Path, default=Path('examples/model-document-workflow.json'))
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    print(json.dumps(check(args.runtime_state, args.state, args.run_id,
                           json.loads(args.config.read_text()), args.verify), indent=2))
