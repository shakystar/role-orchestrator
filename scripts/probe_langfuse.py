"""Actual local graph/Go fixture work, optional OTLP export, independent REST readback.

Client RPC spans measure Python-to-Go round trips, not Go internal execution.
No model calls or prompt/response payloads are exported.
"""
import argparse
import base64
import json
from pathlib import Path
import sys
import time
import urllib.request
import urllib.error
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from role_orchestrator.graph import build
from role_orchestrator.runtime import Runtime
from role_orchestrator.observability import Observability, local_exporter


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--runtime', type=Path, required=True)
    p.add_argument('--state', type=Path, required=True)
    p.add_argument('--env-file', type=Path, default=Path('infra/langfuse/.env'))
    p.add_argument('--endpoint', default='http://localhost:3300')
    p.add_argument('--expect-unavailable', action='store_true')
    args = p.parse_args()
    config = dict(line.split('=', 1) for line in args.env_file.read_text().splitlines() if line and not line.startswith('#'))
    exporter = local_exporter(args.endpoint, config['LANGFUSE_INIT_PROJECT_PUBLIC_KEY'], config['LANGFUSE_INIT_PROJECT_SECRET_KEY'])
    if not args.expect_unavailable:
        with urllib.request.urlopen(args.endpoint.rstrip('/')+'/api/public/health', timeout=5) as ready:
            if json.load(ready).get('status') != 'OK':
                raise RuntimeError('Langfuse is not ready; wait before starting a new probe')
    args.state.mkdir(parents=True, exist_ok=False)
    observer = Observability(exporter)
    run_id = str(uuid.uuid4())
    runtime = Runtime(str(args.runtime.resolve()), args.state.resolve(), 'fixture', 'codex')
    class Instrumented:
        def execute(self, request):
            with observer.span('runtime.rpc.client', task_id=request['id'], role=request['role'], backend='fixture', synthetic=True) as span:
                response = runtime.execute(request)
                span.set_attribute('pilot.cached', response['cached'])
                return response
    try:
        with observer.span('pipeline.probe', run_id=run_id, synthetic=True, backend='fixture') as root:
            trace_id = format(root.get_span_context().trace_id, '032x')
            with SqliteSaver.from_conn_string(str(args.state / 'graph.sqlite')) as saver:
                graph = build(Instrumented(), saver, 'fixture')
                graph_config = {'configurable': {'thread_id': run_id}}
                graph.invoke({'proposal': {}, 'review': {}, 'revisions': 0, 'calls': 0, 'events': [], 'outcome': ''}, graph_config)
                final = graph.invoke(Command(resume=True), graph_config)
                assert final['calls'] == 4 and final['outcome'] == 'accepted'
    finally:
        runtime.close()
        metrics = observer.close()
    report = {'trace_id': trace_id, 'completed_tasks': final['calls'], 'model_calls': 0, 'transport': metrics, 'readback': False}
    report_path = args.state / 'report.json'
    report_path.write_text(json.dumps(report, indent=2)+'\n')
    if args.expect_unavailable:
        assert metrics['failed'] > 0 and metrics['exported'] == 0
    else:
        assert metrics['exported'] == 5 and metrics['failed'] == 0, metrics
        auth = base64.b64encode((config['LANGFUSE_INIT_PROJECT_PUBLIC_KEY']+':'+config['LANGFUSE_INIT_PROJECT_SECRET_KEY']).encode()).decode()
        last_status = None
        for _ in range(30):
            req = urllib.request.Request(args.endpoint.rstrip('/')+'/api/public/v2/observations?traceId='+trace_id+'&limit=20&fields=core,basic,time,io', headers={'Authorization': 'Basic '+auth})
            try:
                with urllib.request.urlopen(req, timeout=3) as response:
                    data = json.load(response)
                observations = data.get('data', [])
                if len(observations) == 5:
                    roots = [o for o in observations if o['name'] == 'pipeline.probe']
                    children = [o for o in observations if o['name'] == 'runtime.rpc.client']
                    assert len(roots) == 1 and len(children) == 4
                    assert all(o['traceId'] == trace_id for o in observations)
                    assert all(o['parentObservationId'] == roots[0]['id'] for o in children)
                    assert all(o.get('input') is None and o.get('output') is None for o in observations)
                    assert all(o.get('endTime') is not None for o in observations)
                    report['parentage_verified'] = True
                    report['payloads_absent'] = True
                    report['readback'] = True
                    report['observations'] = len(observations)
                    break
            except urllib.error.HTTPError as error:
                last_status = error.code
                if error.code not in (404, 500, 503):
                    raise RuntimeError(f'readback HTTP {error.code}') from None
            time.sleep(1)
        assert report['readback'], f'trace not queryable yet, last status={last_status}'
    (args.state / 'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
