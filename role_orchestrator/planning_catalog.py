"""Pinned Git catalog and native planning-team-v1 contract."""
import os
import re

from .contracts import IDENTIFIER, MODEL_NAMES, strict_json, supported_route
from .role_contracts import canonical, contract_instruction, digest
from .roster import AssignmentError, git

PROFILE = 'planning-team-v1'
PATH = 'docs/team/planning-teams.json'
KEY = re.compile(r'[a-z][a-z0-9]{0,23}(?:-[a-z0-9]{1,12})?')
TEAM_KEY = re.compile(KEY.pattern + r'(?:-[a-z0-9]{1,12})?')


def need(ok, message):
    if not ok:
        raise ValueError(message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def key(value):
    return isinstance(value, str) and KEY.fullmatch(value) is not None


def fields(value, names):
    return isinstance(value, dict) and set(value) == set(names.split())


def load(lab, revision, team_id, expected=None):
    need(re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', revision), 'full_commit_sha_required')
    need(git(lab, 'rev-parse', '--verify', revision + '^{commit}').strip() == revision,
         'full_commit_sha_required')
    raw = git(lab, 'show', revision + ':' + PATH)
    sha = digest(raw.encode())
    need(expected is None or expected == sha, 'catalog_hash_mismatch')
    catalog = strict_json(raw)
    need(fields(catalog, 'version teams') and type(catalog['version']) is int
         and catalog['version'] == 1 and isinstance(catalog['teams'], dict), 'invalid_catalog')
    need(isinstance(team_id, str) and TEAM_KEY.fullmatch(team_id)
         and team_id in catalog['teams'], 'unknown_team')
    team = catalog['teams'][team_id]
    need(fields(team, 'lead members goals max_steps') and type(team['max_steps']) is int
         and 1 <= team['max_steps'] <= 32 and isinstance(team['members'], dict)
         and isinstance(team['goals'], dict), 'invalid_team')
    need(key(team['lead']) and team['lead'] in team['members'], 'invalid_lead')
    need(IDENTIFIER.fullmatch(team_id + '--' + str(team['max_steps'])), 'invalid_team_task_id')
    for member, binding in team['members'].items():
        need(key(member) and IDENTIFIER.fullmatch(member)
             and fields(binding, 'role_id role_version instruction model effort')
             and key(binding['role_id']) and key(binding['role_version'])
             and text(binding['instruction']) and text(binding['model']) and text(binding['effort'])
             and supported_route(MODEL_NAMES.get(binding['model'], binding['model']),
                                 binding['effort']), 'invalid_binding')
    for goal, approval in team['goals'].items():
        need(key(goal) and fields(approval, 'text uids') and text(approval['text'])
             and isinstance(approval['uids'], list) and bool(approval['uids'])
             and all(type(uid) is int and uid >= 0 for uid in approval['uids'])
             and len(set(approval['uids'])) == len(approval['uids']), 'invalid_approval')
    return sha, team


def bound_plan(args, revision, version, *, context=None):
    team_id, _, step = args.task.partition('--')
    try:
        need(IDENTIFIER.fullmatch(args.task) and re.fullmatch(r'[1-9]|[12][0-9]|3[0-2]', step),
             'invalid_planning_task')
        sha, team = load(args.lab, revision, team_id)
        need(int(step) <= team['max_steps'] and args.member in team['members'], 'invalid_team_route')
        binding = team['members'][args.member]
        if context is not None:
            principal = context.get('principal')
            need(fields(principal, 'team goal') and principal['team'] == team_id
                 and key(principal['goal']), 'invalid_planning_principal')
            approval = team['goals'].get(principal['goal'])
            need(approval is not None and os.getuid() in approval['uids']
                 and context.get('approved_goal') == approval['text'], 'unapproved_goal_or_user')
    except ValueError as exc:
        raise AssignmentError(str(exc)) from None
    lead = args.member == team['lead']
    contract = dict(version=1, role=binding['role_id'], member=args.member, profile=PROFILE,
                    inputs={'context': 'object'}, kind='planning-decision' if lead else 'planning-artifact',
                    outputs={'decision' if lead else 'artifact': 'object'},
                    source=revision + ':' + PATH, schema_source=PROFILE,
                    catalog_sha256=sha, team_id=team_id, role_version=binding['role_version'])
    contract['sha256'] = digest(canonical(contract))
    return dict(version=version, revision=revision, backend=args.backend, member_id=args.member,
                task_id=args.task, handoff_id=binding['role_id'], expected_role_version=binding['role_version'],
                contract=contract, instruction=contract_instruction(binding['instruction'], contract),
                desired={'model': MODEL_NAMES.get(binding['model'], binding['model']),
                         'effort': binding['effort']})
