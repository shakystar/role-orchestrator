"""Revision-local parent records for the independent review broker."""
import fcntl
import os
from contextlib import contextmanager
from types import SimpleNamespace

from . import assignment, review_team as broker
from .role_contracts import digest

Hold = broker.Hold
KINDS = set(broker.SPECIALISTS) | {'patch'}


@contextmanager
def locked(path, readonly):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if readonly else fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def specification(value, backend):
    required = {'team', 'revision', 'evidence'}
    allowed = required | {'kind', 'stateful', 'authors'}
    if not isinstance(value, dict) or not required <= value.keys() <= allowed:
        raise ValueError('invalid case.review fields')
    if value['team'] != broker.TEAM or type(value['revision']) is not int or value['revision'] < 1:
        raise ValueError('case.review requires independent-review and a positive integer revision')
    if not isinstance(value['evidence'], dict):
        raise ValueError('case.review.evidence must be an object')
    evidence = dict(value['evidence'])
    fixtures = evidence.pop('fixtures', None)
    if 'fixtures' in value['evidence'] and (backend != 'fixture' or not isinstance(fixtures, dict)):
        raise ValueError('review fixtures require the fixture backend and a member-output object')
    kind = value.get('kind')
    if kind is not None and (not isinstance(kind, str) or kind not in KINDS):
        raise ValueError('unknown case.review.kind')
    signals = KINDS.intersection(evidence)
    if kind is None and len(signals) == 1:
        kind = next(iter(signals))
    stateful = value.get('stateful', evidence.get('stateful'))
    if stateful is not None and type(stateful) is not bool:
        raise ValueError('case.review.stateful must be boolean')
    if 'stateful' in evidence and type(evidence['stateful']) is not bool:
        raise ValueError('evidence.stateful must be boolean')
    if value.get('stateful') is not None and evidence.get('stateful', stateful) != stateful:
        raise ValueError('conflicting stateful declarations')
    authors = value.get('authors')
    if 'authors' in value and (not isinstance(authors, list) or not authors
            or not all(broker.matches(broker.MEMBER_ID, item) for item in authors)
            or len(authors) != len(set(authors))):
        raise ValueError('case.review.authors must contain distinct member IDs')
    missing = 'kind' if kind is None else 'stateful' if kind == 'patch' and stateful is None else None
    return SimpleNamespace(revision=value['revision'], kind=kind, stateful=bool(stateful),
                           authors=authors, evidence=evidence, fixtures=fixtures, missing=missing,
                           spec_sha256=digest(broker.encode(value)))


def location(args, spec):
    return args.state / broker.TEAM / str(spec.revision)


def missing_result(case_id, spec):
    return {'case_id': case_id, 'revision': spec.revision, 'state': 'needs_input',
            'required': 'case.review.' + spec.missing, 'owner': 'case.review submitter'}


def save(path, value):
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        broker.write_record(path, value, fd)
    finally:
        os.close(fd)


def reconcile(args, case_id, spec):
    root = location(args, spec)
    path = root / 'intent.json'
    if not path.exists():
        return {'case_id': case_id, 'revision': spec.revision, 'state': 'not_started'} if args.action == 'status' else None
    intent = broker.read_record(path)
    if intent['parent']['spec_sha256'] != spec.spec_sha256:
        raise ValueError('review input changed; use a new review revision')
    return finish(args, root, intent)


def start(args, case_id, spec, criterion, author_input, author, patch_hash):
    root = location(args, spec)
    previous = None
    if spec.revision > 1:
        previous_path = root.parent / str(spec.revision - 1) / 'intent.json'
        if not previous_path.is_file():
            raise ValueError('previous review revision is missing')
        old = broker.read_record(previous_path)
        previous = {'revision': spec.revision - 1, 'input_sha256': old['parent']['input_sha256']}
    artifact = author['artifact']
    member = author.get('actual_member_id')
    if (not broker.matches(broker.MEMBER_ID, member) or artifact['member_id'] != member
            or not broker.matches(broker.SHA256, author.get('receipt_sha256'))):
        raise ValueError('successful author receipt lacks author or receipt identity')
    if spec.authors is not None and set(spec.authors) != {member}:
        raise ValueError('submitted authors differ from successful author receipts')
    parent = {'case_id': case_id, 'revision': spec.revision, 'previous': previous,
              'spec_sha256': spec.spec_sha256, 'roster_sha': args.revision}
    reference = {key: artifact[key] for key in ('task_id', 'member_id', 'run_id', 'attempt_id')}
    reference.update(receipt_sha256=author['receipt_sha256'], output_sha256=artifact['output_sha256'])
    candidate = artifact['payload'].get('candidate_sha')
    evidence = {'review': spec.evidence, 'criterion': criterion, 'source': author_input['source'],
                'base_sha': author_input['base_sha'], 'patch_sha256': patch_hash, 'author_receipt': reference,
                'candidate_sha': candidate, 'authored_artifact': artifact['payload'], 'parent_task_id': case_id, 'revision': spec.revision}
    request_input = {'team': broker.TEAM, 'task': f'Review {spec.kind} for {case_id} revision {spec.revision}.',
                     'kind': spec.kind, 'stateful': spec.stateful, 'authors': [member], 'evidence': evidence}
    if spec.fixtures is not None:
        request_input['fixtures'] = spec.fixtures
    route = broker.validate_request(request_input, args.backend)
    contracts = {}
    for reviewer in route:
        plan = assignment.make_plan(SimpleNamespace(lab=args.lab, revision=args.revision,
            member=reviewer, task=reviewer, backend=args.backend, profile=broker.PROFILE))
        if plan.get('contract', {}).get('profile') != broker.PROFILE:
            raise ValueError('roster does not supply required review role: ' + reviewer)
        contracts[reviewer] = {k: plan[k] for k in (
            'contract', 'model', 'reasoning_effort', 'expected_role_version', 'handoff_id') if k in plan}
    parent.update(review_kind=spec.kind, stateful=spec.stateful, authors=[member], route=route,
                  assignment_contracts=contracts, artifact_sha256=patch_hash, evidence=evidence, input_sha256=digest(broker.encode(request_input)))
    intent = {'version': 1, 'profile': broker.PROFILE, 'parent': parent, 'input': request_input,
              'route': route, 'lab': str(args.lab.resolve()), 'runtime': str(args.runtime.resolve()),
              'revision': args.revision, 'backend': args.backend}
    root.mkdir(mode=0o700, parents=True)
    save(root / 'intent.json', intent)
    return finish(args, root, intent)


def finish(args, root, intent):
    parent = intent['parent']
    path = root / 'result.json'
    recorded = path.exists()
    if recorded and broker.read_record(path).get('delegation') != parent:
        raise Hold('saved parent result does not match its review intent')
    if (intent['lab'] != str(args.lab.resolve()) or intent['runtime'] != str(args.runtime.resolve())
            or intent['revision'] != args.revision or intent['backend'] != args.backend):
        raise ValueError('saved review execution context differs')
    try:
        team, _ = broker.run_review(root / 'team', intent, readonly=True)
        if team['status'] == 'pending' and args.action == 'run' and not recorded:
            team, _ = broker.run_review(root / 'team', intent)
    except (Hold, OSError) as exc:
        team = {'status': 'hold', 'reason': str(exc), 'artifacts': []}
    if team['status'] == 'pending' and recorded:
        team = {**team, 'status': 'hold', 'reason': 'saved parent result has incomplete team state'}
    state = team.get('decision', team['status']) if team['status'] != 'hold' else 'hold'
    result = {'case_id': parent['case_id'], 'revision': parent['revision'], 'stage': 'independent_review',
              'state': state, 'team': team, 'delegation': parent}
    if args.action == 'run' and not recorded and state != 'pending':
        save(path, result)
        recorded = True
    return {**result, 'parent_recorded': recorded}
