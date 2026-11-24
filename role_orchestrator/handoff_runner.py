"""Stateful CLI for one producer-to-consumer handoff.

Provision STATE/runtime first. Producer and consumer are plan positions;
the assigned member ID is the runtime role lookup key.
All writers for this handoff must use the same state-directory lock.

The inspector adapter expects JSON containing an absent/observed discriminator
and, when observed, receipt and wire-request bytes from one run/attempt-bound
inspection. Check the request, task and actual actor, and hash the receipt
bytes. The attempt ID is the store's receipt key, not a receipt content hash. Unknown evidence fails closed. Fixture replacement data is
synthetic and is used only after a verified fixture execution.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from pathlib import Path

from .attempts import AttemptReader, terminal_failure
from .contracts import attempt_id, strict_json as load, supported_route
from .handoffs import Handoffs
from .role_contracts import contract_instruction, output_payload, role_contract, validate_inputs, validate_output

from .runtime import Runtime, load_identity


ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', re.ASCII)
OPS = {
    'publish-initial': ({'payload'}, set()),
    'validate': (set(), {'feedback', 'reviewed_ref'}),
    'accept-review': ({'reviewed_ref'}, set()),
    'repair': ({'request_id'}, {'fixture'}),
    'consume': (set(), {'fixture'}),
    'status': (set(), set()),
}


def dump(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def keys(value, required, optional=()):
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - set(optional):
        raise ValueError('unexpected or missing object fields')


def ident(value, label):
    if not isinstance(value, str) or ID.fullmatch(value) is None:
        raise ValueError(f'invalid {label}')
    return value


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def parse(doc, backend, *, pin=True):
    if not isinstance(doc, dict) or not isinstance(doc.get('op'), str) or doc['op'] not in OPS:
        raise ValueError('unknown operation')
    op = doc['op']
    required, optional = OPS[op]
    keys(doc, {'plan', 'op'} | required, optional)
    if 'fixture' in doc and backend != 'fixture':
        raise ValueError('fixture input requires the fixture backend')
    if op == 'repair':
        ident(doc['request_id'], 'request ID')
    if 'feedback' in doc:
        keys(doc['feedback'], {'criterion', 'field', 'observed', 'required', 'check'})
        if not all(nonempty(value) for value in doc['feedback'].values()):
            raise ValueError('semantic feedback requires five nonempty strings')
        if 'reviewed_ref' not in doc:
            raise ValueError('semantic feedback requires reviewed_ref')
    if 'reviewed_ref' in doc:
        ref = doc['reviewed_ref']
        scope = ('run_id', 'producer', 'kind', 'slot')
        keys(ref, {'revision', 'sha256'}, scope)
        if type(ref['revision']) is not int or ref['revision'] < 1 or not nonempty(ref['sha256']):
            raise ValueError('reviewed_ref requires a positive revision and nonempty sha256')
        for name in scope:
            if name in ref:
                ident(ref[name], f'reviewed_ref {name}')
    plan = doc['plan']
    keys(plan, {'producer', 'consumer', 'slot', 'required_fields'}, {'consumer_input', 'producer_input'})
    if 'producer_input' in plan and not isinstance(plan['producer_input'], dict):
        raise ValueError('producer_input must be the original named assignment input object')
    ident(plan['slot'], 'slot')
    fields = plan['required_fields']
    if not isinstance(fields, list) or not all(nonempty(field) for field in fields) or len(set(fields)) != len(fields):
        raise ValueError('required_fields must be a list of distinct nonempty strings')
    bound = {**plan, 'backend': backend, 'required_fields': sorted(fields)}
    for role in ('producer', 'consumer'):
        actor = plan[role]
        keys(actor, {'member', 'instruction', 'model', 'effort'}, {'profile', 'role_id'})
        ident(actor['member'], f'{role} member ID')
        ident(actor['model'], f'{role} model ID')
        if not supported_route(actor['model'], actor['effort']):
            raise ValueError(f'{role} model or reasoning effort is not allowed')
        if not nonempty(actor['instruction']):
            raise ValueError(f'invalid {role} instruction or reasoning effort')
        bound[role] = {**actor, 'role': role}
        if pin:
            spec = role_contract(actor['member'], actor.get('role_id'), actor.get('profile', 'lab-v1'))
            if spec is not None:
                bound[role]['contract'] = spec
    if bound['producer']['member'] == bound['consumer']['member']:
        raise ValueError('producer and consumer must be distinct')
    if 'consumer_input' in bound:
        binding = bound['consumer_input']
        keys(binding, {'field', 'format', 'context'})
        ident(binding['field'], 'consumer input field')
        if (binding['format'] not in ('payload', 'revision') or not isinstance(binding['context'], dict)
                or binding['field'] in binding['context']):
            raise ValueError('invalid consumer input binding')
    return bound


@contextmanager
def owner(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('state directory already has an active runner') from None
        yield
    finally:
        os.close(fd)


def state_of(value):
    return value.get('state') if isinstance(value, dict) else value


def slot_state(state, slot):
    slots = state.get('slots', {})
    if isinstance(slots, dict):
        return slots.get(slot)
    if isinstance(slots, list):
        return next((item for item in slots if item.get('slot') == slot), None)
    raise ValueError('unrecognized handoff slot state')


def open_request(state, slot, rid):
    entry = slot_state(state, slot) or {}
    requests = entry.get('open_requests', state.get('open_requests', []))
    if isinstance(requests, dict):
        requests = requests.values()
    matches = [item for item in requests if item.get('request_id') == rid and item.get('slot') == slot]
    return matches[0] if len(matches) == 1 else None


def inspect(binary, state_parent, run_id, intent):
    attempt = intent['attempt_id']
    evidence = AttemptReader(binary, state_parent, run_id).bound(attempt, intent['task'])
    if evidence is None:
        return None
    receipt, receipt_sha256 = evidence
    failure = terminal_failure(receipt)
    if failure:
        return {'state': 'failed', 'reason': failure, 'receipt_sha256': receipt_sha256}
    if receipt.get('status') != 'succeeded':
        raise ValueError('receipt_not_terminal')
    result = receipt.get('result')
    if (not isinstance(result, dict) or result.get('actual_member_id') != intent['member']
            or result.get('actual_role_version') != 'v1'
            or not isinstance(result.get('text', ''), str)):
        raise ValueError('invalid receipt result')
    return {'state': 'succeeded', 'text': result.get('text', ''), 'receipt_sha256': receipt_sha256}


class Runner:
    def __init__(self, db, handoffs, runtime, binary, runtime_dir, run_id, plan):
        self.db, self.h, self.runtime = db, handoffs, runtime
        self.binary, self.runtime_dir, self.run_id = binary, runtime_dir, run_id
        self.plan = plan
        self.producer = plan['producer']['member']
        self.consumer = plan['consumer']['member']
        self.slot = plan['slot']

    def consumer_inputs(self, source):
        contract = self.plan['consumer'].get('contract')
        if not contract:
            return None
        binding = self.plan.get('consumer_input')
        payload = source['payload']
        if binding:
            value = payload
            if binding['format'] == 'revision':
                value = dict(**source['artifact'], member_id=self.producer, run_id=self.run_id, artifact=payload)
            payload = {**binding['context'], binding['field']: value}
        validate_inputs(payload, contract)
        return payload

    def producer_output(self, payload):
        contract = self.plan['producer'].get('contract')
        if contract:
            validate_output(payload, contract, self.plan.get('producer_input', {}))

    def run(self, doc):
        op = doc['op']
        if op == 'status':
            state = self.h.state(self.consumer)
            row = self.db.execute('SELECT ack FROM jobs WHERE task_id=?', ('consume',)).fetchone()
            if row and row[0]:
                ack = load(row[0])
                if ack['state'] == 'failed':
                    return {**state, **ack}
                if 'artifact' in ack:
                    artifact = ack['artifact']
                    output = self.h.read(self.consumer, artifact['kind'], revision=artifact['revision'])
                    if output is None or output['artifact'] != artifact:
                        raise ValueError('completed output does not match its receipt')
                    state.update(ack)
                    state['output'] = output
            for pending in (state['open_requests'] if not row else ()):
                if not pending.get('current'):
                    continue
                repair_id = self.h.repair_task_id(pending['request_id'], self.producer, self.slot)
                failure = self.db.execute(
                    "SELECT ack FROM jobs WHERE task_id=? AND json_extract(ack,'$.state')='failed'",
                    (repair_id,)).fetchone()
                if failure:
                    state.update(load(failure[0]))
                    break
            return state
        if op == 'publish-initial':
            return self.h.publish(self.producer, self.slot, doc['payload'], base_revision=0)
        if op == 'validate':
            return self.h.validate(self.consumer, self.slot, reviewed_ref=doc.get('reviewed_ref'),
                                   payload_check=self.producer_output,
                                   **doc.get('feedback', {}))
        if op == 'accept-review':
            return self.h.accept_review(self.consumer, self.slot, doc['reviewed_ref'], payload_check=self.producer_output)
        rid = doc.get('request_id')
        draft = {'fixture': doc['fixture']} if 'fixture' in doc else {}
        if op == 'repair':
            # The original request or an alias may have closed since dispatch.
            repair_id = self.h.repair_task_id(rid, self.producer, self.slot)
            row = self.db.execute('SELECT intent, ack FROM jobs WHERE task_id=?', (repair_id,)).fetchone()
            if row is not None:
                return load(row[1]) if row[1] is not None else self.resume(load(row[0]))
            original = self.h.prepared_claim(self.producer, request_id=rid)
            if original is None:
                request = open_request(self.h.state(self.consumer), self.slot, rid)
                if (request is None or request.get('target_task_id') != self.producer
                        or request.get('external_owner')):
                    return {'state': 'blocked', 'reason': 'request is not open for the bound producer', 'request_id': rid}
                base = request.get('base_revision')
                if type(base) is not int or not 0 <= base < 2**63 - 1:
                    raise ValueError('invalid request base revision')
                producer_contract = self.plan['producer'].get('contract')
                if producer_contract:
                    try:
                        validate_inputs(self.plan.get('producer_input', {}), producer_contract)
                    except ValueError as exc:
                        return {'state': 'blocked', 'reason': str(exc), 'request_id': rid}
                draft.update(request_id=rid, base_revision=base, feedback={
                    key: request[key] for key in ('criterion_id', 'field_path', 'observed',
                                                  'required_change', 'acceptance_check') if key in request})
            else:
                draft = original
            claim = self.h.claim_repair(rid, preparation=draft)
            task_id = claim['repair_task_id']
        else:
            task_id = 'consume'

        # An alias can find the same job. Reconcile its saved intent, including
        # the original request ID, rather than building a new runtime task.
        row = self.db.execute('SELECT intent, ack FROM jobs WHERE task_id=?', (task_id,)).fetchone()
        if row is not None:
            return load(row[1]) if row[1] is not None else self.resume(load(row[0]))

        role = 'producer' if op == 'repair' else 'consumer'
        if op == 'consume':
            if (self.plan['consumer'].get('contract') or self.plan['producer'].get('contract')) and self.h.state(self.consumer)['state'] == 'ready':
                try:
                    source = self.h.read(self.producer, self.slot)
                    self.producer_output(source['payload'])
                    self.consumer_inputs(source)
                except ValueError as exc:
                    return {'state': 'blocked', 'reason': str(exc)}
            claim = self.h.claim_ready(self.consumer, preparation=draft)
        if state_of(claim) not in ('ready', 'uncertain'):
            return claim if isinstance(claim, dict) else {'state': claim}
        # Only a draft committed WITH a new CLI claim can bridge the two DBs.
        # An uncertain legacy or non-CLI claim is not dispatch authority.
        draft = self.h.prepared_claim(self.plan[role]['member'], task_id=task_id)
        if draft is None:
            return {'state': 'uncertain', 'reason': 'claim has no CLI preparation', 'task_id': task_id}
        if op == 'repair':
            rid, base = draft['request_id'], draft['base_revision']
            try:
                current, inputs = self.h.read_repair_inputs(self.producer, self.slot, rid)
            except (ValueError, LookupError, sqlite3.Error):
                return {'state': 'uncertain', 'reason': 'repair artifacts could not be verified', 'request_id': rid}
            if (current['artifact']['revision'] if current is not None else 0) != base:
                return {'state': 'blocked', 'reason': 'request base mismatch', 'request_id': rid}
            body = {'slot': self.slot, 'request_id': rid, 'base_revision': base,
                    'current': current, 'inputs': inputs, 'feedback': draft['feedback']}
            if self.plan['producer'].get('contract'):
                body['assignment_inputs'] = self.plan['producer_input']
        else:
            try:
                current = self.h.read_claimed(self.consumer, self.slot)
            except (ValueError, LookupError, sqlite3.Error):
                return {'state': 'uncertain', 'reason': 'claimed artifact could not be verified'}
            body = {'slot': self.slot, **current}
            if self.plan['consumer'].get('contract'):
                body['inputs'] = self.consumer_inputs(current)
            base = current['artifact']['revision']
        if 'fixture' in draft:
            body['fixture'] = draft['fixture']
        actor = self.plan[role]
        instruction = contract_instruction(actor['instruction'], actor.get('contract'))
        task = {'id': task_id, 'role': actor['member'], 'instruction': instruction, 'input': dump(body)}
        intent = {'op': op, 'request_id': rid, 'base_revision': base, 'task': task,
                  'attempt_id': attempt_id(self.run_id, task_id), 'member': actor['member']}
        intent['phase'] = 'prepared'
        with self.db:
            self.db.execute('INSERT INTO jobs(task_id,intent) VALUES(?,?)', (task_id, dump(intent)))
        return self.resume(intent)

    def resume(self, intent):
        # Old job rows were committed immediately before execute. They may
        # have crossed that boundary even if the inspector finds no receipt.
        phase = intent.get('phase', 'submitted')
        if phase == 'submitted':
            return self.reconcile(intent)
        task = intent['task']
        if phase != 'prepared':
            return {'state': 'uncertain', 'reason': 'unknown job phase', 'task_id': task['id']}
        runtime = self.runtime()  # Initialization failure leaves the job prepared.
        submitted = {**intent, 'phase': 'submitted'}
        with self.db:
            changed = self.db.execute('UPDATE jobs SET intent=? WHERE task_id=? AND intent=? AND ack IS NULL',
                                      (dump(submitted), task['id'], dump(intent))).rowcount
            if changed != 1:
                raise ValueError('dispatch preparation changed')
        # From here even a crash just BEFORE execute is ambiguous. Never retry.
        try:
            runtime.execute(task)
        except Exception:
            pass  # The bridge may have committed a receipt before raising.
        return self.reconcile(submitted)

    def reconcile(self, intent):
        task_id = intent['task']['id']
        ref = {'task_id': task_id, 'attempt_id': intent['attempt_id']}
        try:
            proof = inspect(self.binary, self.runtime_dir, self.run_id, intent)
        except (KeyError, ValueError, TypeError, OSError, subprocess.SubprocessError):
            return {'state': 'uncertain', 'reason': 'no verified successful receipt', **ref}
        if proof is None:
            return {'state': 'uncertain', 'reason': 'receipt absent after intent', **ref}
        receipt_id = intent['attempt_id']
        ack = {**ref, 'receipt_id': receipt_id, 'receipt_sha256': proof['receipt_sha256']}
        if intent.get('request_id'):
            ack['request_id'] = intent['request_id']
        if proof['state'] == 'failed':
            return self.record(task_id, {**ack, 'state': 'failed', 'reason': proof['reason']})
        ack.update(actual_member_id=intent['member'], actual_role_version='v1')
        contract = self.plan['producer' if intent['op'] == 'repair' else 'consumer'].get('contract')
        if contract is not None:
            try:
                body = load(intent['task']['input'])
                inputs = (body.get('assignment_inputs', {}) if intent['op'] == 'repair'
                          else body.get('inputs', body.get('payload', {})))
                payload = output_payload(proof['text'], contract, inputs)
            except (ValueError, TypeError, KeyError, RecursionError):
                return self.record(task_id, {**ack, 'state': 'failed', 'reason': 'invalid_role_output'})
            ack['contract_sha256'] = contract['sha256']
        try:
            if intent['op'] == 'repair':
                if contract is None:
                    payload = load(proof['text'])
                artifact = self.h.publish(self.producer, self.slot, payload,
                                          base_revision=intent['base_revision'], request_id=intent['request_id'])
                ack.update(state='published', request_id=intent['request_id'],
                           base_revision=intent['base_revision'], artifact=artifact)
            else:
                if contract is not None:
                    ack['artifact'] = self.h.finish_with_output(self.consumer, receipt_id, contract['kind'], payload)
                elif state_of(state := self.h.state(self.consumer)) == 'completed':
                    if state.get('receipt_id') != receipt_id:
                        raise ValueError('completion receipt mismatch')
                elif state_of(self.h.finish(self.consumer, receipt_id)) != 'completed':
                    raise ValueError('handoff store did not complete consumer')
                ack['state'] = 'completed'
        except (ValueError, TypeError, LookupError, RuntimeError, sqlite3.Error):
            return {'state': 'uncertain', 'reason': 'verified receipt could not be applied', **ref}
        # A crash before this commit leaves a reconcilable intent. Publication
        # replays exactly; a completed consume is not finished a second time.
        return self.record(task_id, ack)

    def record(self, task_id, ack):
        with self.db:
            self.db.execute('UPDATE jobs SET ack=? WHERE task_id=? AND ack IS NULL', (dump(ack), task_id))
        return ack


def invoke(args, doc):
    requested = parse(doc, args.backend, pin=False)
    directory = Path(args.state).resolve()
    binary = Path(args.runtime).resolve(strict=True)
    status_only = doc['op'] == 'status'
    db_path = directory / 'runner.sqlite3'
    handoffs_path = directory / 'handoffs.sqlite3'
    if status_only:
        if not db_path.is_file() or not handoffs_path.is_file():
            raise ValueError('no existing handoff state')
        if not (directory / 'run-identity.json').exists():
            raise ValueError('no existing run identity')
    else:
        directory.mkdir(parents=True, exist_ok=True)
    db_target = db_path.as_uri() + '?mode=ro' if status_only else db_path
    with owner(directory), closing(sqlite3.connect(db_target, uri=status_only)) as db:
        if (directory / 'runtime').is_symlink():
            raise ValueError('runtime state directory must not be a symlink')
        if status_only:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manifest'").fetchone() is None:
                raise ValueError('no existing handoff manifest')
        else:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS manifest (id INTEGER PRIMARY KEY CHECK(id=1), run_id TEXT NOT NULL, plan TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS jobs (task_id TEXT PRIMARY KEY, intent TEXT NOT NULL, ack TEXT)')
        row = db.execute('SELECT run_id, plan FROM manifest WHERE id=1').fetchone()
        if status_only and row is None:
            raise ValueError('no existing handoff manifest')
        if row is None:
            plan = parse(doc, args.backend)
        else:
            plan = load(row[1])
            comparable = {**plan, **{side: {k: v for k, v in plan[side].items() if k != 'contract'}
                                     for side in ('producer', 'consumer')}}
            if requested != comparable:
                raise ValueError('plan differs from the immutable manifest')
        encoded = dump(plan)
        actor = plan['consumer' if doc['op'] == 'consume' else 'producer']
        run_id = ident(load_identity(directory, create=not status_only)['run_id'], 'runtime run ID')
        if row is not None and run_id != row[0]:
            raise ValueError('runtime run differs from the immutable manifest')

        runtime = None

        def runtime_for_dispatch():
            nonlocal runtime
            if runtime is None:
                runtime = Runtime(str(binary), directory, args.backend, 'codex',
                                  model=actor['model'], reasoning_effort=actor['effort'])
                if runtime.identity['run_id'] != run_id:
                    raise ValueError('runtime run differs from the immutable manifest')
            return runtime

        open_handoffs = Handoffs.open_readonly if status_only else Handoffs
        try:
            with closing(open_handoffs(handoffs_path, run_id)) as h:
                runner = Runner(db, h, runtime_for_dispatch, binary, directory, run_id, plan)
                if not status_only:
                    fields = {name: None for name in plan['required_fields']}
                    fields.update(plan['producer'].get('contract', {}).get('outputs', {}))
                    h.bind(runner.consumer, runner.slot, runner.producer, fields)
                    if row is None:
                        # The binding commits separately. If the manifest write fails,
                        # rerunning the same plan can reuse the binding without dispatching twice.
                        with db:
                            db.execute('INSERT INTO manifest VALUES(1,?,?)', (run_id, encoded))
                            inserted = db.execute('SELECT run_id, plan FROM manifest WHERE id=1').fetchone()
                            if inserted != (run_id, encoded):
                                raise ValueError('new handoff manifest could not be verified')
                return runner.run(doc)
        finally:
            if runtime is not None:
                runtime.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--runtime', required=True)
    parser.add_argument('--backend', required=True, choices=('fixture', 'codex'))
    args = parser.parse_args(argv)
    try:
        data = sys.stdin.buffer.read(1_048_577)
        if len(data) > 1_048_576:
            raise ValueError('input exceeds 1 MiB')
        result = invoke(args, load(data))
    except Exception as exc:
        # Do not echo a runtime exception, prompt, receipt or local path.
        reason = str(exc) if type(exc) is ValueError else type(exc).__name__
        print(dump({'state': 'error', 'reason': reason}))
        return 2
    print(dump(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

