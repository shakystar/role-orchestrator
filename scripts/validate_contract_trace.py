"""Bounded contract/trace acceptance probe; fixture by default, live exactly one call."""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from role_orchestrator.runtime import Runtime


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--state',type=Path,required=True)
    p.add_argument('--runtime',type=Path,required=True)
    p.add_argument('--telemetry-env',type=Path,default=Path('infra/langfuse/.env'))
    p.add_argument('--live',action='store_true')
    p.add_argument('--codex',default='codex')
    args=p.parse_args()
    state=args.state.resolve()
    state.mkdir(parents=True,exist_ok=False)
    binary=str(args.runtime.resolve())
    env_path=args.telemetry_env.resolve()
    request={'id':'smoke-1','role':'smoke','instruction':'Return only JSON {"ok":true}. Do not use tools.','input':'Synthetic acknowledgement. Return the requested JSON.'}
    if args.live:
        client=Runtime(binary,state,'codex',args.codex,telemetry_env=env_path)
        try:
            result=client.execute(request)
            assert json.loads(result['text'])=={'ok':True} and not result['cached']
        finally:
            client.close()
        backend='codex'
        expected_exec,expected_requests=1,2
    else:
        command=[sys.executable,'-m','role_orchestrator']
        flags=['--state',str(state),'--runtime',binary,'--telemetry-env',str(env_path)]
        first=json.loads(subprocess.check_output(command+['start']+flags,text=True))
        assert first['calls']==2 and first['next']==['checkpoint']
        final=json.loads(subprocess.check_output(command+['resume']+flags,text=True))
        assert final['calls']==4 and final['next']==[]
        receipt=next(json.loads(path.read_text()) for path in (state/'runtime/tasks').glob('*.json'))
        task=receipt['task']
        from role_orchestrator.graph import INSTRUCTIONS
        request={'id':task['id'],'role':task['role'],'instruction':INSTRUCTIONS[task['role']],'input':task['input']}
        backend='fixture'
        expected_exec,expected_requests=4,5
    # New process, deliberately unusable model binary: cache must not start it.
    client=Runtime(binary,state,backend,'/nonexistent/codex',telemetry_env=env_path)
    try:
        cached=client.execute(request)
        assert cached['cached'] and all(v==0 for v in cached['stats'].values())
    finally:
        client.close()
    identity=json.loads((state/'run-identity.json').read_text())
    py=[json.loads(line) for line in (state/'python-spans.jsonl').read_text().splitlines()]
    go=[json.loads(line) for line in (state/'runtime/bridge-spans.jsonl').read_text().splitlines()]
    expected_ids={s['span_id'] for s in py+go}
    assert len(expected_ids)==len(py+go)
    assert all(s['trace_id']==identity['trace_id'] for s in py+go)
    assert sum(s['name']=='runtime.execute' for s in go)==expected_exec
    assert sum(s['name']=='runtime.request' for s in go)==expected_requests
    settings=dict(line.split('=',1) for line in env_path.read_text().splitlines() if line and not line.startswith('#'))
    base=settings.get('LANGFUSE_BASE_URL','http://localhost:3300')
    auth=base64.b64encode((settings['LANGFUSE_INIT_PROJECT_PUBLIC_KEY']+':'+settings['LANGFUSE_INIT_PROJECT_SECRET_KEY']).encode()).decode()
    observed=[]
    for _ in range(30):
        req=urllib.request.Request(base+'/api/public/v2/observations?traceId='+identity['trace_id']+'&limit=100&fields=core,basic,time,io,metadata',headers={'Authorization':'Basic '+auth})
        with urllib.request.urlopen(req,timeout=5) as response:
            observed=json.load(response)['data']
        if {o['id'] for o in observed}==expected_ids:
            break
        time.sleep(1)
    assert {o['id'] for o in observed}==expected_ids, 'stored span IDs do not match local records'
    by_id={o['id']:o for o in observed}
    for span in py+go:
        actual=by_id[span['span_id']]
        parent=span['parent_span_id']
        assert actual['parentObservationId']==(None if not parent or parent=='0'*16 else parent)
        assert actual.get('input') is None and actual.get('output') is None
    assert sum(o['name']=='runtime.execute' for o in observed)==expected_exec
    report={'mode':'live-one-call' if args.live else 'fixture-resume','run_id':identity['run_id'],'trace_id':identity['trace_id'],'observations':len(observed),'runtime_executions':expected_exec,'runtime_requests':expected_requests,'cache_replay':True,'cross_process_parentage':True,'payloads_absent':True,'model_calls':1 if args.live else 0,'usage_and_cost':'not in scope'}
    if args.live:
        statuses=[json.loads(line) for line in (state/'runtime-status.jsonl').read_text().splitlines() if line.startswith('{')]
        assert sum(s.get('phase')=='completed' for s in statuses)==1
    (state/'acceptance.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':
    main()
