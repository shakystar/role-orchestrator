"""Prepare a Python change using the declared lab roles and their upstream outputs."""
import re
import subprocess

from .role_contracts import canonical, validate_fields


PLANNING = {'scenario': 'acl-001', 'identity': 'acl-002', 'windows': 'acl-003',
            'vectors': 'acl-004', 'oracle': 'acl-005', 'regression': 'acl-008'}
VECTOR_REVISIONS = 2


def validate_goal(case, backend):
    required = {'case_id', 'goal', 'base_sha', 'source'}
    if not required <= case.keys() <= required | {'fixtures'}:
        raise ValueError('invalid goal development fields')
    validate_fields(case, {'goal': 'string', 'base_sha': 'string', 'source.path': 'string'})
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', case['base_sha']):
        raise ValueError('full source commit required')
    source = case['source']
    files = source.get('files')
    if source.keys() - {'path', 'files'} or ('files' in source and
            (not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files))):
        raise ValueError('invalid source file selection')
    repairs = {f'{stage}-r{i}' for stage in ('vectors', 'oracle') for i in range(1, VECTOR_REVISIONS + 1)}
    repairs.update({'reviewer-r1', 'reviewer-r2', 'evidence-r1', 'evidence-r2'})
    if 'fixtures' in case and (backend != 'fixture' or not isinstance(case['fixtures'], dict)
            or not set(PLANNING) | {'author', 'reviewer'} <= case['fixtures'].keys()
            or case['fixtures'].keys() - set(PLANNING) - repairs - {'author', 'reviewer', 'evidence'}):
        raise ValueError('invalid goal fixtures')


def git(source, *args):
    return subprocess.check_output(['git', '--no-replace-objects', '-C', source, *args],
                                   stderr=subprocess.PIPE, timeout=30)


def inventory(case):
    source = case['source']['path']
    sha = case['base_sha']
    if git(source, 'rev-parse', '--verify', sha + '^{commit}').decode().strip() != sha:
        raise ValueError('source revision is not a commit')
    files = []
    for entry in git(source, 'ls-tree', '-rz', sha).split(b'\0'):
        if entry.startswith((b'100644 blob ', b'100755 blob ')):
            name = entry.split(b'\t', 1)[1].decode()
            if name.endswith('.py'):
                files.append(name)
    selected = case['source'].get('files', files)
    if not selected or not set(selected) <= set(files):
        raise ValueError('source files must be tracked Python files at the base commit')
    return selected


def read_files(case, files):
    content = {}
    for name in files:
        ref = case['base_sha'] + ':' + name
        if int(git(case['source']['path'], 'cat-file', '-s', ref)) > 32 * 1024:
            raise ValueError('selected source exceeds assignment input limit')
        content[name] = git(case['source']['path'], 'cat-file', 'blob', ref).decode()
    if len(canonical(content)) > 36 * 1024:
        raise ValueError('selected source exceeds assignment input limit')
    return {'base_sha': case['base_sha'], 'files': content}


def review_vectors(dispatch, report, payloads, original_inputs):
    for revision in range(VECTOR_REVISIONS + 1):
        review = report['oracle']['payload']
        if review.get('vectors_sha256') != report['vectors']['sha256']:
            break
        if review.get('verdict') == 'accept':
            return None
        if review.get('verdict') != 'revise' or revision == VECTOR_REVISIONS:
            break
        history = report.setdefault('revisions', {})
        for name in ('vectors', 'oracle'):
            history[report[name]['task_id']] = report[name]
        correction = dict(previous=payloads['vectors'], reviewed_sha256=report['vectors']['sha256'],
                          feedback=review, review_receipt=report['oracle']['receipt_sha256'])
        for name in ('vectors', 'oracle'):
            task = f'{name}-r{revision + 1}'
            if name == 'vectors':
                inputs = {key: payloads[key] for key in ('scenario', 'identity', 'windows')}
                inputs.update(correction=correction, instructions='Revise the previous vectors for all '
                              'actionable review feedback. Preserve the scenario criterion_id.')
            else:
                inputs = dict(original_inputs, vectors=payloads['vectors'], vectors_sha256=report['vectors']['sha256'],
                              prior_review=correction, instructions='Independently judge this revised set of '
                              'test vectors against the goal. This stage approves test design before implementation. '
                              'Do not require a patch or execution results here. Return verdict accept, revise or '
                              'blocked, and copy the revised vectors_sha256 exactly.')
            result = dispatch(task, PLANNING[name], inputs, 'lab-v1')
            if result['status'] != 'succeeded':
                return {**report, 'stage': task, 'state': result['status'], 'detail': result}
            report[name] = history[task] = result['artifact']
            payloads[name] = result['artifact']['payload']
            if name == 'vectors' and payloads[name]['criterion_id'] != payloads['scenario']['criterion_id']:
                return {**report, 'stage': task, 'state': 'blocked', 'reason': 'scenario criterion mismatch'}
    return {**report, 'stage': report['oracle']['task_id'], 'state': 'blocked', 'reason': 'vectors not approved'}


def prepare(case, dispatch, report):
    files = inventory(case)
    goal = {'text': case['goal'], 'case_id': case['case_id']}
    baseline = {'base_sha': case['base_sha'], 'available_files': files}
    payloads = {}
    source = None
    for name, member in PLANNING.items():
        if name == 'scenario':
            inputs = dict(goal=goal, baseline=baseline,
                          instructions='Also list relevant Python source or test paths in source_files. '
                          'Use only paths from available_files; keep the scenario concise.')
        elif name == 'identity':
            chosen = case['source'].get('files', payloads['scenario'].get('source_files', []))
            if not isinstance(chosen, list) or not chosen or not all(isinstance(f, str) and f in files for f in chosen):
                raise ValueError('scenario must select pinned source files')
            source = read_files(case, chosen)
            inputs = dict(scenario=payloads['scenario'], source=source)
        elif name == 'windows':
            inputs = dict(identity=payloads['identity'], source=source)
        elif name == 'vectors':
            inputs = {key: payloads[key] for key in ('scenario', 'identity', 'windows')}
            inputs['instructions'] = 'Copy scenario.criterion_id exactly. Cover the requested observable behavior.'
        elif name == 'oracle':
            inputs = {key: payloads[key] for key in ('scenario', 'vectors', 'windows')}
            inputs.update(goal=goal, vectors_sha256=report['vectors']['sha256'],
                          instructions='Add verdict (accept, revise, blocked) and copy vectors_sha256 exactly. '
                          'Review the cases against the goal. Return concise JSON.')
        else:
            inputs = dict(vectors=payloads['vectors'], oracle=payloads['oracle'], source=source,
                          instructions='Write an applicable unified diff adding a small self-contained Python '
                          'unittest under tests/. Use an executable command that returns 1 on assertion failure '
                          'on the baseline and 0 on the corrected code. Copy source.base_sha exactly. Do not '
                          'change product code. Use valid unified diff hunk counts and end the diff with LF.')
        result = dispatch(name, member, inputs, 'lab-v1')
        if result['status'] != 'succeeded':
            return None, {**report, 'stage': name, 'state': result['status'], 'detail': result}
        artifact = result['artifact']
        report[name] = artifact
        payload = payloads[name] = artifact['payload']
        if name == 'vectors' and payload['criterion_id'] != payloads['scenario']['criterion_id']:
            return None, {**report, 'stage': name, 'state': 'blocked', 'reason': 'scenario criterion mismatch'}
        if name == 'oracle':
            stopped = review_vectors(dispatch, report, payloads, inputs)
            if stopped is not None:
                return None, stopped
        if name == 'regression' and payload['base_sha'] != case['base_sha']:
            return None, {**report, 'stage': name, 'state': 'blocked', 'reason': 'regression base mismatch'}
    criterion = {'id': payloads['scenario']['criterion_id']}
    author = dict(goal=case['goal'], base_sha=case['base_sha'], source=source,
                  regression=payloads['regression'], acceptance=dict(scenario=payloads['scenario'],
                  approved_vectors=payloads['vectors'], oracle=payloads['oracle']),
                  output_contract={'diff': 'Return an applicable unified diff of the minimal product code '
                                   'fix only. Preserve untouched behavior, exact hunk counts and final LF.'})
    return (criterion, author), None
