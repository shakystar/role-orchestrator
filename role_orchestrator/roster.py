"""Resolve a development assignment from a pinned Git roster and handoff table."""
import os
import re
import subprocess

from .contracts import IDENTIFIER as ID, MODEL_NAMES, supported_route
from .role_contracts import contract_instruction, role_contract


class AssignmentError(Exception):
    pass


def literal(cell):
    return cell[1:-1].strip() if len(cell) >= 2 and cell[0] == cell[-1] == '`' else cell


def select_row(markdown, key, code, handoff=False):
    found = []
    fence = ''
    for line in map(str.strip, markdown.splitlines()):
        mark = re.match(r'`{3,}|~{3,}', line)
        if fence:
            if mark and mark[0].startswith(fence) and not line[mark.end():].strip():
                fence = ''
            continue
        if mark:
            fence = mark[0]
        elif line.startswith('|'):
            cells = re.split(r'(?<!\\)\|', line)[1:]
            label = cells[0].partition('/')[0] if handoff else cells[0]
            if literal(label.strip()) == key:
                found.append([cell.strip().replace(r'\|', '|') for cell in cells[:-1]] if cells[-1] == '' else [])
    if len(found) != 1 or len(found[0]) != 4 or not all(found[0]):
        raise AssignmentError(code)
    return found[0]


def git(lab, *args):
    try:
        return subprocess.run(
            ['git', '--no-pager', '--no-replace-objects', '-C', str(lab), *args],
            check=True, capture_output=True, timeout=15,
            env={**os.environ, 'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0'},
        ).stdout.decode('utf-8')
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise AssignmentError('lab_revision_read_failed') from None


def make_plan(args, version=2):
    revision = args.revision.lower()
    if (not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', revision)
            or git(args.lab, 'rev-parse', '--verify', revision + '^{commit}').strip() != revision):
        raise AssignmentError('full_commit_sha_required')
    if not ID.fullmatch(args.member) or not ID.fullmatch(args.task):
        raise AssignmentError('invalid_member_or_task_id')
    if args.profile == 'planning-team-v1':
        from .planning_catalog import bound_plan
        return bound_plan(args, revision, version)
    roster = git(args.lab, 'show', revision + ':docs/team/roster.md')
    _, name, duty, handoff = select_row(roster, args.member, 'invalid_or_ambiguous_roster_row')
    handoff = literal(handoff)
    if not ID.fullmatch(handoff):
        raise AssignmentError('invalid_handoff_id')
    specs = git(args.lab, 'show', revision + ':docs/features/development-handoffs.md')
    label, inputs, outputs, desired = select_row(specs, handoff, 'invalid_or_ambiguous_handoff_row', handoff=True)
    title = label.partition('/')[2].strip()
    if not title:
        raise AssignmentError('invalid_handoff_title')
    model, sep, effort = map(str.strip, desired.partition('/'))
    if not sep or model not in MODEL_NAMES or not supported_route(MODEL_NAMES[model], effort):
        raise AssignmentError('unsupported_desired_model_or_effort')
    try:
        contract = role_contract(args.member, handoff, args.profile)
    except ValueError as exc:
        raise AssignmentError(str(exc)) from None
    instruction = f'{title}\n담당: {name} — {duty}\n입력: {inputs}\n산출물: {outputs}'
    return dict(version=version, revision=revision, backend=args.backend, member_id=args.member,
                task_id=args.task, handoff_id=handoff, expected_role_version='v1',
                contract=contract, instruction=contract_instruction(instruction, contract),
                desired={'model': MODEL_NAMES[model], 'effort': effort})
