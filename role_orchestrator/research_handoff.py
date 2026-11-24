"""Run a request or signal through pinned native research assignments."""
import argparse
import fcntl
import os
from pathlib import Path
import re
import sys

from .contracts import strict_json
from .planning_catalog import PROFILE, fields, load, need, text
from .planning_team import link, native, save, state_directory
from .role_contracts import canonical, digest
from .roster import AssignmentError

TEAM = 'research-evidence-route'
LIMIT = 262144
ROLES = dict(observe='research-observer', draft='research-synthesizer',
             review='research-reviewer')
STEPS = dict(intake=1, observe=1, draft=2, review=3, synthesize=4, decision=2)
META = {'time', 'method', 'scope', 'denominator', 'deduplicated'}
PROTOCOL = {
    'intake': 'Return action (start_review or hold), reason and case_sha256. No task creation.',
    'observe': 'Return observations, hypotheses, signals, unknowns, falsifiers (text arrays), '
               'and needs (array of {ref,field} for unknown evidence facts).',
    'draft': 'Return claims keyed by ID: statement, support and counter (evidence-ID arrays), '
             'requires ({ref,field} array), alternatives and falsifiers (text arrays), limits '
             'and reproduction (text). Declare dependencies for each inference, compare alternatives, '
             'and distinguish observations from inference and unverified reproduction.',
    'review': 'Return draft_sha256, conflict (boolean) and opinions keyed by opinion ID: '
              'claim, verdict (support, oppose or undetermined), reason and missing ({ref,field} array). '
              'Cover every claim, test counterexplanations and declare any involvement.',
    'synthesize': 'Return draft_sha256, dispositions keyed by opinion ID (acknowledge or retain_dissent), '
                  'and next_question. Preserve negative or uncertain opinions as dissent; no rewritten claims.',
    'decision': 'Return packet_sha256, action (retain, hold or select_followup), reason and claims '
                '(IDs of eligible claims only for select_followup, empty otherwise). Do not create work '
                'or change priorities, approved goals, constraints or access.',
}


def token(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value)


def array(value, predicate=text):
    return isinstance(value, list) and all(map(predicate, value))


def mapping(value):
    return isinstance(value, dict) and bool(value) and all(map(token, value))


def case_input(case):
    need(fields(case, 'id kind source question constraints access_scope synthetic evidence contributors')
         and token(case['id']) and case['kind'] in ('request', 'signal')
         and text(case['question']) and text(case['access_scope'])
         and array(case['constraints']) and array(case['contributors'], token)
         and type(case['synthetic']) is bool and mapping(case['evidence']), 'invalid_case')
    source_fields = ('requester' if case['kind'] == 'request' else 'observed_at channel scope hypothesis')
    source = case['source']
    need(fields(source, 'ref text author ' + source_fields)
         and all(map(text, source.values())), 'invalid_source')
    for item in case['evidence'].values():
        need(fields(item, 'source owner text synthetic facts')
             and all(text(item[k]) for k in ('source', 'owner', 'text'))
             and type(item['synthetic']) is bool and (case['synthetic'] or not item['synthetic'])
             and mapping(item['facts']) and META <= item['facts'].keys(), 'invalid_evidence')


def dependencies(items, evidence):
    need(isinstance(items, list), 'invalid_dependencies')
    missing = set()
    for item in items:
        need(fields(item, 'ref field') and token(item['ref']) and token(item['field'])
             and item['ref'] in evidence, 'invalid_dependency')
        facts = evidence[item['ref']]['facts']
        if facts.get(item['field']) is None:
            missing.add((item['ref'], item['field']))
    return missing


def requests(missing, evidence):
    return [dict(ref=ref, field=field, to=evidence[ref]['owner'], status='requested_in_packet')
            for ref, field in sorted(missing)]


def authors(case, receipts):
    return sorted(set(case['contributors']) | {case['source']['author']}
                  | {e['owner'] for e in case['evidence'].values()}
                  | {receipts[p]['member_id'] for p in ('observe', 'draft')})


def apply(phase, value, state, receipts):
    case, data = state['case'], state['data']
    evidence = case['evidence']
    if phase == 'intake':
        need(fields(value, 'action reason case_sha256') and text(value['reason'])
             and value['case_sha256'] == digest(canonical(case))
             and value['action'] in ('start_review', 'hold'), 'invalid_intake')
        return {'intake': value}, value if value['action'] == 'hold' else None
    if phase == 'observe':
        names = 'observations hypotheses signals unknowns falsifiers'
        need(fields(value, names + ' needs') and all(array(value[k]) for k in names.split()),
             'invalid_observation')
        missing = dependencies(value['needs'], evidence)
        return {'observation': value, 'upstream': requests(missing, evidence)}, None
    if phase == 'draft':
        need(fields(value, 'claims') and mapping(value['claims']), 'invalid_draft')
        for claim in value['claims'].values():
            need(fields(claim, 'statement support counter requires alternatives falsifiers limits reproduction')
                 and all(text(claim[k]) for k in ('statement', 'limits', 'reproduction'))
                 and all(array(claim[k]) and claim[k] for k in ('alternatives', 'falsifiers'))
                 and all(array(claim[k], token) and set(claim[k]) <= evidence.keys()
                         for k in ('support', 'counter')), 'invalid_claim')
            dependencies(claim['requires'], evidence)
        draft = dict(case_sha256=digest(canonical(case)), evidence_refs=sorted(evidence),
                     authors=authors(case, receipts), claims=value['claims'])
        return {'draft': draft, 'draft_sha256': digest(canonical(draft))}, None
    if phase == 'review':
        need(fields(value, 'draft_sha256 conflict opinions') and value['conflict'] is False
             and value['draft_sha256'] == data['draft_sha256'] and mapping(value['opinions']),
             'invalid_or_conflicted_review')
        claims = data['draft']['claims']
        covered = set()
        for opinion in value['opinions'].values():
            need(fields(opinion, 'claim verdict reason missing') and token(opinion['claim'])
                 and opinion['claim'] in claims and text(opinion['reason'])
                 and opinion['verdict'] in ('support', 'oppose', 'undetermined'), 'invalid_opinion')
            dependencies(opinion['missing'], evidence)
            covered.add(opinion['claim'])
        need(covered == claims.keys(), 'incomplete_review')
        return {'review': value}, None
    if phase == 'synthesize':
        opinions = data['review']['opinions']
        need(fields(value, 'draft_sha256 dispositions next_question') and text(value['next_question'])
             and value['draft_sha256'] == data['draft_sha256']
             and isinstance(value['dispositions'], dict) and value['dispositions'].keys() == opinions.keys(),
             'invalid_synthesis')
        gates = {}
        for cid, claim in data['draft']['claims'].items():
            missing = dependencies(claim['requires'], evidence)
            holds = [] if claim['support'] else ['no_support']
            ids = []
            for oid, opinion in sorted(opinions.items()):
                if opinion['claim'] != cid:
                    continue
                ids.append(oid)
                dissent = opinion['verdict'] != 'support'
                disposition = value['dispositions'][oid]
                need(disposition == 'retain_dissent' or (not dissent and disposition == 'acknowledge'),
                     'dissent_not_preserved')
                if dissent:
                    holds.append('independent_review:' + oid)
                missing.update(dependencies(opinion['missing'], evidence))
            if missing:
                holds.append('missing_evidence')
            gates[cid] = dict(status='revision_hold' if holds else 'eligible', reasons=holds,
                              opinions=ids, upstream=requests(missing, evidence))
        packet = dict(case=case, **data, synthesis=value, gates=gates)
        return {'packet': dict(packet, receipts=receipts), 'packet_sha256': digest(canonical(packet))}, None
    eligible = {cid for cid, gate in data['packet']['gates'].items() if gate['status'] == 'eligible'}
    need(fields(value, 'packet_sha256 action reason claims') and text(value['reason'])
         and value['packet_sha256'] == data['packet_sha256'] and array(value['claims'], token)
         and len(set(value['claims'])) == len(value['claims'])
         and ((value['action'] == 'select_followup' and value['claims']
               and set(value['claims']) <= eligible)
              or (value['action'] in ('retain', 'hold') and not value['claims'])), 'invalid_product_decision')
    return {'decision': value}, value


def routes(research, planning, planning_id):
    members = research['members']
    need(len(members) == 3 and research['max_steps'] >= 4 and planning['max_steps'] >= 2
         and planning['lead'] not in members, 'invalid_research_route')
    result = {}
    for phase, role in ROLES.items():
        matched = [m for m, binding in members.items() if binding['role_id'] == role]
        need(len(matched) == 1, 'invalid_research_phase_binding')
        result[phase] = (TEAM, matched[0])
    need(result['draft'][1] == research['lead'], 'invalid_research_lead')
    result['synthesize'] = result['draft']
    result['intake'] = result['decision'] = (planning_id, planning['lead'])
    return result


def advance(args, fd, state, route, approved, teams):
    phases = list(STEPS) if state['case']['kind'] == 'signal' else list(STEPS)[1:]
    wait = None
    while state['branch'] is None and len(state['receipts']) < len(phases):
        phase = phases[len(state['receipts'])]
        team, member = route[phase]
        fresh = state['pending'] is None
        if fresh:
            if phase == 'review' and member in {a.strip().casefold() for a in state['data']['draft']['authors']}:
                state['branch'] = dict(action='reassign', reason='reviewer_participated', phase=phase)
                save(fd, state)
                break
            goal, goal_text = approved[team]
            context = dict(principal=dict(team=team, goal=goal), approved_goal=goal_text,
                           research_goal=approved[TEAM][1], case=state['case'], phase=phase,
                           case_sha256=digest(canonical(state['case'])), prior=state['data'],
                           receipts=state['receipts'], protocol=PROTOCOL[phase])
            work = {'context': context}
            if state['fixtures'] is not None:
                work['fixture'] = state['fixtures'][phase]
            state['pending'] = dict(phase=phase, task_id=f'{team}--{STEPS[phase]}', member_id=member,
                                    input=work)
            save(fd, state)
        current = state['pending']
        need(current['phase'] == phase, 'checkpoint_phase_mismatch')
        try:
            receipt = link(args, current, native(args, current, fresh, state_fd=fd))
        except (OSError, ValueError, AssignmentError) as exc:
            wait = dict(action='hold', reason=str(exc), phase=phase, task_id=current['task_id'])
            break
        proof = {k: v for k, v in receipt.items() if k != 'artifact'}
        proof.update(case_id=state['case']['id'], role_version=teams[team]['members'][member]['role_version'])
        # Do not mutate dictionaries already committed into the packet or native input.
        receipts = dict(state['receipts'], **{phase: proof})
        envelope = 'decision' if member == teams[team]['lead'] else 'artifact'
        try:
            value = receipt['artifact']['payload'][envelope]
            need(len(canonical(value)) <= LIMIT, 'stage_result_too_large')
            updates, branch = apply(phase, value, state, receipts)
        except (KeyError, ValueError, RecursionError) as exc:
            updates, branch = {}, dict(action='hold', phase=phase, reason=str(exc), terminal=True)
        state.update(data=dict(state['data'], **updates), receipts=receipts, branch=branch, pending=None)
        save(fd, state)
        if args.one_step:
            break
    next_phase = (phases[len(state['receipts'])]
                  if state['branch'] is None and len(state['receipts']) < len(phases) else None)
    return dict(case=state['case'], pin=state['pin'], branch=state['branch'] or wait,
                next_phase=next_phase, receipts=state['receipts'], data=state['data'])


def run(args, incoming):
    need(args.planning_team != TEAM, 'planning_team_must_be_separate')
    teams, approved = {}, {}
    uid = os.getuid()
    for team_id, goal in ((TEAM, args.research_goal), (args.planning_team, args.planning_goal)):
        _, team = load(args.lab, args.revision, team_id, args.catalog_sha256)
        approval = team['goals'].get(goal)
        need(approval is not None and uid in approval['uids'], 'unapproved_goal_or_user')
        teams[team_id], approved[team_id] = team, (goal, approval['text'])
    route = routes(teams[TEAM], teams[args.planning_team], args.planning_team)
    pin = dict(schema=1, profile=PROFILE, catalog_sha256=args.catalog_sha256, revision=args.revision,
               uid=uid, lab=args.lab, runtime=args.runtime, backend=args.backend,
               research_goal=args.research_goal, planning_team=args.planning_team, planning_goal=args.planning_goal)
    with state_directory(args.state, uid) as fd, os.fdopen(
        os.open('.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd), 'wb'
    ) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            checkpoint = os.open('team.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        except FileNotFoundError:
            need(not (set(os.listdir(fd)) - {'.lock', 'team.tmp'}), 'unrecognized_state')
            with_fixtures = args.backend == 'fixture'
            need(fields(incoming, 'case fixtures' if with_fixtures else 'case'), 'invalid_case_envelope')
            case_input(incoming['case'])
            fixtures = incoming.get('fixtures')
            if with_fixtures:
                phases = set(STEPS) - ({'intake'} if incoming['case']['kind'] == 'request' else set())
                need(incoming['case']['synthetic'] and isinstance(fixtures, dict)
                     and fixtures.keys() == phases and all(isinstance(v, dict) for v in fixtures.values()),
                     'invalid_native_fixtures')
            state = dict(pin=pin, case=incoming['case'], fixtures=fixtures,
                         data={}, receipts={}, pending=None, branch=None)
            save(fd, state)
        else:
            with os.fdopen(checkpoint, encoding='utf-8') as handle:
                state = strict_json(handle.read())
            need(state.get('pin') == pin, 'checkpoint_pin_mismatch')
            need(incoming is None, 'resume_requires_empty_input')
        return advance(args, fd, state, route, approved, teams)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('lab', 'revision', 'runtime', 'state', 'catalog-sha256', 'planning-team',
                 'planning-goal', 'research-goal'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--backend', choices=('fixture', 'codex'), required=True)
    parser.add_argument('--one-step', action='store_true')
    args = parser.parse_args()
    try:
        args.lab, args.runtime = (str(Path(p).resolve(strict=True)) for p in (args.lab, args.runtime))
        raw = sys.stdin.buffer.read(LIMIT + 1)
        need(len(raw) <= LIMIT, 'input_too_large')
        result = run(args, strict_json(raw.decode()) if raw.strip() else None)
    except (OSError, ValueError, AssignmentError, RecursionError) as exc:
        sys.stdout.buffer.write(canonical({'error': str(exc)}) + b'\n')
        return 2
    sys.stdout.buffer.write(canonical(result) + b'\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
