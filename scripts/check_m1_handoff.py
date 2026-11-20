"""Offline calibration with M1 public CLI and a previously completed handoff."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def run(source, evidence, work):
    work.mkdir(parents=True, exist_ok=False)
    env = {**os.environ, 'PYTHONPATH': str(source), 'INSPECT_EVAL_MODEL': 'mockllm/model'}
    def command(args):
        return subprocess.check_output([sys.executable, *map(str, args)], env=env, cwd=work, text=True)
    def ae(*args):
        return json.loads(command(['-m', 'agent_eval', '--store', work/'store', *args]))

    inputs = work/'input'
    command([source/'examples/m1/create_example.py', inputs])
    def read(name):
        return json.loads((inputs/name).read_text())
    def ref(name, data):
        (inputs/name).write_bytes(data)
        return dict(id=name, path=name, sha256=digest(data))

    baseline = json.loads((evidence/'restart-baseline.json').read_text())
    doc = (evidence/'document.json').read_bytes()
    review = (evidence/'review.json').read_bytes()
    for name, raw in [('document', doc), ('review', review)]:
        if digest(raw) != baseline[f'artifacts/{name}.json']:
            raise ValueError('saved artifact differs from restart baseline')
    expected = digest(doc)
    changed = json.loads(review)
    assert changed['source_sha256'] == expected
    wrong = ('0' if expected[0] != '0' else '1') + expected[1:]
    control = review.replace(expected.encode(), wrong.encode(), 1)
    changed['source_sha256'] = wrong
    assert json.loads(control) == changed

    bundle, exp, manifest = map(read, ['bundle.json','experiment.json','evidence.json'])
    task = bundle['tasks'][0]
    task.update(domain='document-handoff', source='model-1119a saved handoff', inputs=[ref('document.json',doc)],
                evaluation=[], outputs=['review JSON'], completion='Review cites the supplied document.',
                reference_path=[])
    criterion = task['criteria'][0]
    criterion.update(id='source-hash', rule=dict(type='json_equals', source='output',pointer='/source_sha256',value=expected))
    bundle.update(id='handoff-hash',family='handoff')
    exp.update(id='handoff', repetitions=1, hypothesis='Detect one changed hash character.',
               varying_fields=['target.adapter'])
    exp['bundle'] = dict(id=bundle['id'],version='1',sha256=digest(json.dumps(bundle,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()))
    db = sqlite3.connect((evidence/'metrics.sqlite').as_uri()+'?mode=ro', uri=True)
    try:
        finished = [json.loads(b) for b, in db.execute('SELECT body FROM events')]
    finally:
        db.close()
    attempt, = [e['attempt_id'] for e in finished if e['task_id']=='consumer'
                and e['kind']=='attempt.finished' and e['payload']['outcome']=='completed']
    records, runs = [], []
    for cond, raw in zip(exp['conditions'], [review, control]):
        key = cond['id']
        cond['target'].update(adapter='saved-review' if key=='baseline' else 'mutated-control',
                              execution_owner='offline-import',retry_owner='none')
        cond['model'].update(settings={},missing_reason='offline evidence check')
        cond['environment'] = dict.fromkeys(cond['environment'],'offline saved-output calibration')
        r = copy.deepcopy(manifest['runs'][0])
        r.update(id=key, condition=key, attempt_id=attempt if key=='baseline' else 'synthetic-hash-control',environment=cond['environment'])
        runs.append(r)
        records.append(dict(id=key,output=ref(key+'.json',raw),missing_reason=None))
    save(inputs/'source.json',dict(schema_version=1,id='handoff-pair',records=records))
    manifest.update(experiment='handoff',runs=runs)
    manifest['source']['sha256']=digest((inputs/'source.json').read_bytes())
    for verb, name, payload in [('bundle','bundle',bundle),('experiment','experiment',exp),('import','evidence',manifest)]:
        path=inputs/(name+'.json')
        save(path,payload)
        ae(verb,path)
    assert ae('import',inputs/'evidence.json')['duplicate']
    for eid, extra in [('initial',[]),('regrade',['--parent','initial'])]:
        result=ae('evaluate','handoff',eid,'--reviewer','contract-check','--reason','offline hash check',*extra)
        assert result['judgments'] == 2 and result['errors'] == 0
        destination = work / ('report-' + eid)
        ae('report','handoff',eid,'--output',destination)
        report = json.loads((destination/'report.json').read_text())
        assert {row['condition']:row['provisional_success_rate']['value']
                for row in report['conditions']} == {'baseline':1, 'changed':0}
        assert all(row['judgeable'] == 1 for row in report['conditions'])
    print('baseline=met changed=unmet duplicate_import=true regrade=verified')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','evidence','work'):
        parser.add_argument(name,type=Path)
    a=parser.parse_args()
    run(a.source.resolve(),a.evidence.resolve(),a.work.resolve())
