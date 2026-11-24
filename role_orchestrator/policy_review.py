"""Policy binding for the existing independent patch route."""
import json
import os
import re
import stat
import subprocess
import tempfile

from . import assignment
from .role_contracts import canonical, digest, role_contract

POLICY = '9bda89879d1587a74d91aff5d8f9d28c103542f6'
REVIEWER = 'acl-012'
CLAUSES = ('OWN-01', 'SER-01', 'REC-01')
ALLOWED = {'role_orchestrator/review_team.py', 'role_orchestrator/parent_review.py',
           'role_orchestrator/development_case.py'}
OID = re.compile(r'[0-9a-f]{40}')
FILE = 'policy-review.json'


def subject(value):
    if (not isinstance(value, dict) or set(value) != {'base_sha', 'candidate_sha'}
            or any(not isinstance(v, str) or not OID.fullmatch(v) for v in value.values())):
        raise ValueError('patch review requires subject base_sha and candidate_sha commit IDs')
    return dict(value)


def git(repo, *args, limit=32768):
    env = dict(PATH=os.defpath, LC_ALL='C', GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_REPLACE_OBJECTS='1',
               GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0')
    with subprocess.Popen(['git', '-C', str(repo), *args], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        data = process.stdout.read(limit + 1)
        if len(data) > limit:
            process.kill()
            raise ValueError('git evidence exceeds the byte budget')
        if process.wait():
            raise ValueError('cannot read pinned local git objects')
    return data


def commit(repo, oid):
    if not OID.fullmatch(oid) or git(repo, 'cat-file', '-t', oid) != b'commit\n':
        raise ValueError('expected a full local commit ID')


def clauses(lab):
    commit(lab, POLICY)
    lines = git(lab, 'cat-file', 'blob', POLICY + ':docs/development-policy.md',
                limit=131072).decode('utf-8').splitlines()
    headings = [re.match(r'^(#{1,6})\s+(.+)$', line) for line in lines]
    result = {}
    for clause in CLAUSES:
        sections = []
        for index, line in enumerate(lines):
            heading = headings[index]
            label = r'^[`*]*' + clause + r'\b'
            if heading and re.match(label, heading[2]):
                end = next((i for i in range(index + 1, len(lines))
                            if headings[i] and len(headings[i][1]) <= len(heading[1])), len(lines))
                sections.append('\n'.join(lines[index:end]).strip())
            elif re.match(r'^\s*\|\s*[`*]*' + clause + r'[`*]*\s*\|', line):
                sections.append(line.strip())
        if len(sections) != 1 or len(sections[0].encode()) > 8192:
            raise ValueError('missing, ambiguous or oversized pinned clause ' + clause)
        result[clause] = sections[0]
    return result


def inputs(args, declaration):
    if args.subject_repo is None:
        raise ValueError('patch review requires --subject-repo')
    repo = args.subject_repo.resolve(strict=True)
    if repo == args.lab.resolve() or git(repo, 'rev-parse', '--show-prefix') != b'\n':
        raise ValueError('use the role-orchestrator checkout root, separate from --lab')
    base, candidate = declaration['base_sha'], declaration['candidate_sha']
    for oid in (base, candidate):
        commit(repo, oid)
    commit(args.lab, args.revision)
    options = ('diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--no-color',
               '--no-relative', '--ignore-submodules=none')
    raw = git(repo, *options, '--raw', base, candidate, '--').decode('utf-8').splitlines()
    paths = set()
    for line in raw:
        item = re.fullmatch(r':100(?:644|755) 100(?:644|755) [0-9a-f]+ [0-9a-f]+ M\t(.+)', line)
        if not item or item[1] not in ALLOWED:
            raise ValueError('patch contains a path or change type outside the source allowlist')
        paths.add(item[1])
    patch = git(repo, *options, '--patch', '--full-index', '--no-indent-heuristic',
                '--diff-algorithm=myers', '--src-prefix=a/', '--dst-prefix=b/', '--unified=3',
                base, candidate, '--').decode('utf-8')
    anchors = {}
    for line in patch.splitlines():
        if line.startswith('diff --git a/'):
            path = line.split(' b/', 1)[1]
        elif line.startswith('@@ '):
            anchors.setdefault(path, []).append(line.split(' @@', 1)[0] + ' @@')
    if not paths or set(anchors) != paths:
        raise ValueError('patch must contain only allowlisted textual source changes')
    binding = dict(declaration, repository='role-orchestrator', checkout=str(repo), policy_sha=POLICY)
    evidence = dict(subject={k: v for k, v in binding.items() if k != 'checkout'},
                    source=patch, anchors=anchors, clauses=clauses(args.lab))
    return binding, evidence


INSTRUCTIONS = ('Independently assess OWN-01, SER-01 and REC-01 using only these excerpts; '
                'do not inspect files or use tools. acl-012: keep decision/reason/owner. '
                'In reason put a JSON array, one finding per clause, each with clause_id, path, '
                'symbol, observed, expected, impact, evidence (an exact hunk anchor for path), '
                'remediation, owner, and verdict (pass, required, or hold). '
                'Use hold for missing evidence; otherwise return required fixes through R4 '
                'feedback owned by the author, or accept if all pass.')


def reference(artifact, verdict):
    keys = ('task_id', 'member_id', 'run_id', 'attempt_id', 'output_sha256')
    return dict({key: artifact[key] for key in keys}, receipt_sha256=verdict['receipt_sha256'])


def bind(args, spec, author_input, author):
    binding, evidence = inputs(args, spec.subject)
    artifact = author['artifact']
    if (author_input['base_sha'] != binding['base_sha']
            or artifact['payload']['diff'] != evidence['source']):
        raise ValueError('subject base or git diff differs from the signed author artifact')
    plan = assignment.decode((args.state / 'author' / assignment.PLAN).read_bytes())
    binding['author_plan_sha256'] = digest(canonical(plan))
    evidence.update(instructions=INSTRUCTIONS, author_id=artifact['member_id'])
    return binding, evidence


def replay(args, parent):
    binding = parent['policy_binding']
    declaration = {key: binding[key] for key in ('base_sha', 'candidate_sha')}
    current, evidence = inputs(args, declaration)
    current['author_plan_sha256'] = binding['author_plan_sha256']
    if current != binding or parent['evidence'] != dict(
            evidence, instructions=INSTRUCTIONS, author_id=parent['authors'][0]):
        raise ValueError('pinned patch review input changed')
    state = args.state / 'author'
    plan = assignment.decode((state / assignment.PLAN).read_bytes())
    if digest(canonical(plan)) != binding['author_plan_sha256']:
        raise ValueError('saved author plan changed')
    ids = {key: plan[key] for key in ('run_id', 'attempt_id')}
    verdict = assignment.observe(str(args.runtime.resolve()), state, ids, plan)
    if (not verdict or verdict.get('status') != 'succeeded'
            or verdict.get('actual_member_id') != parent['authors'][0]
            or reference(verdict['artifact'], verdict) != parent['author_receipt']
            or verdict['artifact']['payload']['diff'] != evidence['source']
            or digest(evidence['source'].encode()) != parent['artifact_sha256']):
        raise ValueError('native author receipt does not bind this patch')


def findings(evidence, receipt):
    output = receipt['artifact']['payload']
    if receipt['actual_member_id'] != REVIEWER or set(output) != {'decision', 'reason', 'owner'}:
        raise ValueError('unexpected policy review member or envelope')
    if len(output['reason']) > 65536:
        raise ValueError('policy findings exceed the size budget')
    def unique(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError('duplicate policy finding fields')
        return value
    entries = json.loads(output['reason'], object_pairs_hook=unique)
    keys = {'clause_id', 'path', 'symbol', 'observed', 'expected', 'impact',
            'evidence', 'remediation', 'owner', 'verdict'}
    if not isinstance(entries, list) or len(entries) != len(CLAUSES):
        raise ValueError('expected one finding per clause')
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != keys
                or not all(isinstance(v, str) and v.strip() and len(v) <= 2048 for v in entry.values())
                or entry['evidence'] not in evidence['anchors'].get(entry['path'], ())
                or entry['verdict'] not in ('pass', 'required', 'hold')
                or (entry['verdict'] == 'required' and entry['owner'] != evidence['author_id'])):
            raise ValueError('invalid or unbound policy finding')
    if {f['clause_id'] for f in entries} != set(CLAUSES):
        raise ValueError('policy clauses are missing or duplicated')
    flags = {f['verdict'] for f in entries}
    decision = output['decision']
    if ('hold' in flags and decision != 'hold'
            or 'hold' not in flags and 'required' in flags
            and (decision in ('accept', 'hold') or output['owner'] != evidence['author_id'])
            or flags == {'pass'} and decision != 'accept'):
        raise ValueError('native decision does not match policy findings')
    return entries


def link(root, parent, team, readonly):
    receipt = next((a for a in team['artifacts'] if a['actual_member_id'] == REVIEWER), None)
    directory = root / 'evidence'
    path = directory / FILE
    if receipt is None:
        if team['status'] == 'accepted' or os.path.lexists(path):
            raise ValueError('policy review receipt is missing')
        return None
    binding = parent['policy_binding']
    payload = dict(policy_sha=binding['policy_sha'], roster_sha=parent['roster_sha'],
                   case_id=parent['case_id'], revision=parent['revision'],
                   repository=binding['repository'], base_sha=binding['base_sha'],
                   candidate_sha=binding['candidate_sha'], patch_sha256=parent['artifact_sha256'],
                   author_id=parent['authors'][0], author_receipt=parent['author_receipt'],
                   reviewer_id=REVIEWER, run_id=receipt['run_id'], attempt_id=receipt['attempt_id'],
                   receipt_sha256=receipt['receipt_sha256'], output_sha256=receipt['artifact']['sha256'],
                   findings=findings(parent['evidence'], receipt))
    data = canonical(payload)
    if not readonly:
        directory.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            existing = os.open(FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            if readonly:
                raise
            with tempfile.NamedTemporaryFile(dir=directory) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                os.link(stream.name, FILE, dst_dir_fd=fd)
        else:
            with os.fdopen(existing, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or stream.read(len(data) + 1) != data:
                    raise ValueError('policy evidence bytes differ from the verified receipt')
        if not readonly:
            os.fsync(fd)
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
    finally:
        os.close(fd)
    return {'path': 'evidence/' + FILE, 'sha256': digest(data)}
