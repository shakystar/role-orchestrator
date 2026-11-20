"""Score one saved handoff through agent-eval's CLI, with optional semantic grading."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_requirements(path):
    contract = json.loads(path.read_text())
    if not isinstance(contract, dict) or set(contract) != {'version', 'request', 'required_items'}:
        raise ValueError('expected version, request and required_items')
    items = contract['required_items']
    if (type(contract['version']) is not int or contract['version'] != 1
            or not isinstance(contract['request'], str) or not contract['request'].strip()
            or not isinstance(items, list) or not 1 <= len(items) <= 8
            or any(not isinstance(item, str) or not item.strip() for item in items)
            or len({item.strip() for item in items}) != len(items)):
        raise ValueError('supply a nonempty request and 1..8 distinct required items for v1')
    return contract


def semantic_grade(store, run_id, evaluation, model, criterion='grounding-v1'):
    suffix = 'grounding' if criterion == 'grounding-v1' else criterion
    grade_id = evaluation + '-' + suffix
    command = [sys.executable, '-m', 'agent_eval', '--store', str(store)]
    grade = subprocess.run([*command, 'semantic-grade', run_id, 'review', criterion,
                            grade_id, '--model', model], text=True, capture_output=True)
    result = dict(evaluation_id=grade_id, model=model, criterion=criterion, outcome='unknown')
    if grade.returncode not in (0, 1):
        return {**result, 'processing': 'error', 'error_kind': 'grader_cli', 'exit_code': grade.returncode}

    raw = subprocess.check_output([*command, 'reliability', grade_id], text=True)
    report = json.loads(raw)
    case, = report['cases']
    if report['evaluation'] != grade_id or case['run'] != 'consumer':
        raise ValueError('semantic evaluation identity mismatch')
    filename = 'semantic-report.json' if criterion == 'grounding-v1' else criterion + '-report.json'
    (store.parent / filename).write_text(raw)
    return {**result, 'processing': case['processing'], 'outcome': case['draft'],
            'error_kind': case['error_kind'], 'failure': case['failure'], 'report': filename}


def evaluate(evidence, semantic_model=None, requirements=None):
    if requirements and not semantic_model:
        raise ValueError('requirements grading needs a semantic model')
    contract = read_requirements(requirements) if requirements else None
    manifest = json.loads((evidence / 'manifest.json').read_text())
    if manifest['version'] != 1:
        raise ValueError('unsupported evidence version')
    run_id = manifest['run_id']
    attempt = manifest['tasks']['consumer']['attempt_id']
    evaluation = 'eval-' + uuid4().hex
    work = evidence.parent / 'handoff-evaluations' / evaluation
    work.mkdir(parents=True, mode=0o700)
    store = work / 'store'

    def cli(*args):
        raw = subprocess.check_output([sys.executable, '-m', 'agent_eval', '--store', str(store), *args], text=True)
        return json.loads(raw)

    def file_ref(name, raw):
        (work / name).write_bytes(raw)
        return {'id': name, 'path': name, 'sha256': digest(raw)}

    def register(name, payload):
        path = work / (name + '.json')
        path.write_text(json.dumps(payload, indent=2))
        cli(name, str(path))

    refs = {}
    for task, name in [('producer', 'document.json'), ('consumer', 'review.json')]:
        ref = manifest['tasks'][task]['artifact']
        data = (evidence / name).read_bytes()
        if ref['id'] != name or digest(data) != ref['sha256']:
            raise ValueError('snapshot integrity check failed for ' + name)
        refs[task] = file_ref(name, data)

    criterion = {'id': 'source-hash', 'version': '1', 'kind': 'artifact', 'required': True,
                 'rule': {'type': 'json_equals', 'source': 'output', 'pointer': '/source_sha256',
                          'value': refs['producer']['sha256']}}
    task = dict(id='review', version='1', domain='document-handoff', source=run_id,
                inputs=[refs['producer']], evaluation=[], allowed_actions=['read supplied document'],
                outputs=['review JSON'], completion='Review cites the supplied document.',
                criteria=[criterion], reference_path=[], validity='unreviewed')
    semantic_criterion = 'grounding-v1'
    if semantic_model:
        rubric_dir = Path(__file__).resolve().parents[1] / 'evaluation/grounding'
        try:
            document = json.loads((work / 'document.json').read_bytes())
        except (UnicodeDecodeError, ValueError):
            document = None  # Older snapshots may carry a plain-text source.
        if isinstance(document, dict) and 'task_contract' in document:
            semantic_criterion = 'proposal-v1'
            rubric = rubric_dir / 'proposal-v1.txt'
        else:
            rubric = rubric_dir / 'rubric-v1.txt'
        task['evaluation'] = [file_ref(semantic_criterion, rubric.read_bytes())]
    coverage = []
    if contract:
        policy_path = Path(__file__).resolve().parents[1] / 'evaluation/completeness/rubric-v1.txt'
        policy = policy_path.read_text()
        contract_ref = file_ref('requirements.json', json.dumps(contract, ensure_ascii=False).encode())
        for index, item in enumerate(contract['required_items'], 1):
            key = f'completeness-v1-{index}'
            data = json.dumps({'request': contract['request'], 'required_item': item}, ensure_ascii=False)
            task['evaluation'].append(file_ref(key, (policy + data).encode()))
            coverage.append((key, item))
    bundle = dict(schema_version=1, id='handoff', version='1', family='handoff', split='validation', tasks=[task])
    register('bundle', bundle)
    canonical = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    environment = dict.fromkeys(('initial_state', 'reset', 'isolation', 'shared_state', 'session_policy'),
                                'not captured in handoff snapshot')
    condition = dict(id='observed', environment=environment, skills=[], roles=[{'id':'consumer','version':'v1'}],
                     model=dict(name=None, settings={}, missing_reason='not captured'),
                     target=dict(adapter='saved-output', version='1', execution_owner='role-runtime',
                                 retry_owner='role-runtime', auth_method='not captured'))
    budget = dict(limit=None, unit='USD', mode='observational', missing_reason='not captured')
    register('experiment', dict(schema_version=1, id=run_id, hypothesis='Review references the completed document.',
                                bundle=dict(id='handoff', version='1', sha256=digest(canonical)), baseline='observed',
                                conditions=[condition], repetitions=1, budget=dict(execution=budget,evaluation=budget),
                                varying_fields=[], comparison_policy='same-task-criteria-budget'))
    source = dict(schema_version=1, id='saved-review', records=[dict(id='consumer', output=refs['consumer'], missing_reason=None)])
    measurements = {key: dict(value=None, unit=unit, scope='execution', completeness='missing', missing_reason='not exported')
                    for key, unit in [('time','seconds'), ('tokens','tokens'), ('cost','USD')]}
    run = dict(id='consumer', task='review', condition='observed', repeat=1, attempt_id=attempt,
               execution_status='completed', environment=environment, started=None, ended=None,
               receipt=attempt, measurements=measurements)
    register('import', dict(schema_version=1, id='saved-handoff', experiment=run_id, format='saved-output-v1',
                            source=file_ref('source.json', json.dumps(source).encode()), runs=[run]))
    cli('evaluate', run_id, evaluation, '--reviewer', 'handoff-check', '--reason', 'post-run source reference check')
    cli('report', run_id, evaluation, '--output', str(work / 'report'))
    report = json.loads((work / 'report/report.json').read_text())
    row, = report['conditions'][0]['runs']
    if report['evaluation'] != evaluation or row['attempt_id'] != attempt:
        raise ValueError('evaluation report identity mismatch')
    result = dict(run_id=run_id, attempt_id=attempt, evaluation_id=evaluation, criterion='source-hash',
                  outcome=row['outcome'], report='report/report.json', evidence_sha256=refs['consumer']['sha256'])
    if semantic_model:
        result['semantic'] = semantic_grade(store, run_id, evaluation, semantic_model, semantic_criterion)
    if contract:
        items = []
        for key, item in coverage:
            judgment = semantic_grade(store, run_id, evaluation, semantic_model, key)
            items.append(dict(required_item=item, **judgment))
        outcomes = {item['outcome'] for item in items}
        overall = 'unmet' if 'unmet' in outcomes else 'unknown' if 'unknown' in outcomes else 'met'
        result['completeness'] = dict(request=contract['request'], requirements_sha256=contract_ref['sha256'],
                                      outcome=overall, items=items)
    (work / 'result.json').write_text(json.dumps(result, indent=2)+'\n')
    return work, result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    parser.add_argument('--semantic-model', help='Also grade the review reason with this agent-eval model')
    parser.add_argument('--requirements', type=Path, help='Also assess each required item in this v1 JSON contract')
    args = parser.parse_args()
    if args.requirements and not args.semantic_model:
        parser.error('--requirements needs --semantic-model')
    work, result = evaluate(args.evidence.resolve(), args.semantic_model, args.requirements)
    print(json.dumps({**result, 'result_file': str(work / 'result.json')}))
    outcomes = [result['outcome']]
    if args.semantic_model:
        outcomes.append(result['semantic']['outcome'])
    if args.requirements:
        outcomes.extend(item['outcome'] for item in result['completeness']['items'])
    sys.exit(2 if 'unknown' in outcomes else 0 if all(v == 'met' for v in outcomes) else 1)
