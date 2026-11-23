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
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from pathlib import Path

from .contracts import attempt_id
from .handoffs import Handoffs
from os import environ

from .runtime import Runtime, load_identity


ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', re.ASCII)
EFFORTS = {'low', 'medium', 'high'}
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


def load(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result

    result = json.loads(data, object_pairs_hook=pairs)
    dump(result)  # Reject non-finite numbers, including float overflow.
    return result


def keys(value, required, optional=()):
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - set(optional):
        raise ValueError('unexpected or missing object fields')


def ident(value, label):
    if not isinstance(value, str) or ID.fullmatch(value) is None:
        raise ValueError(f'invalid {label}')
    return value


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def parse(doc, backend):
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
    keys(plan, {'producer', 'consumer', 'slot', 'required_fields'})
    ident(plan['slot'], 'slot')
    fields = plan['required_fields']
    if not isinstance(fields, list) or not all(nonempty(field) for field in fields) or len(set(fields)) != len(fields):
        raise ValueError('required_fields must be a list of distinct nonempty strings')
    bound = {**plan, 'backend': backend, 'required_fields': sorted(fields)}
    for role in ('producer', 'consumer'):
        actor = plan[role]
        keys(actor, {'member', 'instruction', 'model', 'effort'})
        ident(actor['member'], f'{role} member ID')
        ident(actor['model'], f'{role} model ID')
        allowed_models = {'gpt-6.2-sol', 'gpt-6.2-astra'}
        if actor['model'] not in allowed_models:
            raise ValueError(f'{role} model ID is not allowed')
        if not nonempty(actor['instruction']) or not isinstance(actor['effort'], str) or actor['effort'] not in EFFORTS:
            raise ValueError(f'invalid {role} instruction or reasoning effort')
        bound[role] = {**actor, 'role': role}
    if bound['producer']['member'] == bound['consumer']['member']:
        raise ValueError('producer and consumer must be distinct')
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
    from .attempts import AttemptReader

    attempt = intent['attempt_id']
    evidence = AttemptReader(binary, state_parent, run_id).read(attempt)
    if evidence is None:
        return None
    wire_bytes, receipt_bytes = evidence
    wire, receipt = load(wire_bytes), load(receipt_bytes)
    if not isinstance(wire, dict) or not isinstance(receipt, dict):
        raise ValueError('invalid inspector evidence')
    task = intent['task']
    if (type(wire.get('schema_version')) is not int or wire['schema_version'] != 1
            or wire.get('run_id') != run_id or wire.get('attempt_id') != attempt
            or any(name not in wire or wire[name] != value
                   for name, value in task.items())):
        raise ValueError('wire request does not match intent')
    receipt_task = receipt.get('task')
    if (receipt.get('status') != 'succeeded'
            or ('run_id' in receipt and receipt['run_id'] != run_id)
            or ('attempt_id' in receipt and receipt['attempt_id'] != attempt)
            or not isinstance(receipt_task, dict)
            or any(receipt_task.get(name) != task[name] for name in ('id', 'role', 'input'))
            or receipt_task.get('version') != 'v1'):
        raise ValueError('receipt does not match intent')
    result = receipt.get('result')
    if (not isinstance(result, dict) or result.get('actual_member_id') != intent['member']
            or result.get('actual_role_version') != 'v1'
            or not isinstance(result.get('text'), str)):
        raise ValueError('invalid receipt result')
    return attempt, 'v1', result, hashlib.sha256(receipt_bytes).hexdigest()


class Runner:
    def __init__(self, db, handoffs, runtime, binary, runtime_dir, run_id, plan):
        self.db, self.h, self.runtime = db, handoffs, runtime
        self.binary, self.runtime_dir, self.run_id = binary, runtime_dir, run_id
        self.plan = plan
        self.producer = plan['producer']['member']
        self.consumer = plan['consumer']['member']
        self.slot = plan['slot']

    def run(self, doc):
        op = doc['op']
        if op == 'status':
            return self.h.state(self.consumer)
        if op == 'publish-initial':
            return self.h.publish(self.producer, self.slot, doc['payload'], base_revision=0)
        if op == 'validate':
            return self.h.validate(self.consumer, self.slot, reviewed_ref=doc.get('reviewed_ref'),
                                   **doc.get('feedback', {}))
        if op == 'accept-review':
            return self.h.accept_review(self.consumer, self.slot, doc['reviewed_ref'])
        rid = doc.get('request_id')
        draft = {'fixture': doc['fixture']} if 'fixture' in doc else {}
        if op == 'repair':
            # The original request may have closed since the job was recorded.
            for saved_intent, saved_ack in self.db.execute('SELECT intent, ack FROM jobs').fetchall():
                intent = load(saved_intent)
                if intent['op'] == 'repair' and intent['request_id'] == rid:
                    return load(saved_ack) if saved_ack is not None else self.resume(intent)
            original = self.h.prepared_claim(self.producer, request_id=rid)
            if original is None:
                request = open_request(self.h.state(self.consumer), self.slot, rid)
                if (request is None or request.get('target_task_id') != self.producer
                        or request.get('external_owner')):
                    return {'state': 'blocked', 'reason': 'request is not open for the bound producer', 'request_id': rid}
                base = request.get('base_revision')
                if type(base) is not int or not 0 <= base < 2**63 - 1:
                    raise ValueError('invalid request base revision')
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
        else:
            try:
                current = self.h.read_claimed(self.consumer, self.slot)
            except (ValueError, LookupError, sqlite3.Error):
                return {'state': 'uncertain', 'reason': 'claimed artifact could not be verified'}
            body = {'slot': self.slot, **current}
            base = current['artifact']['revision']
        if 'fixture' in draft:
            body['fixture'] = draft['fixture']
        actor = self.plan[role]
        task = {'id': task_id, 'role': actor['member'], 'instruction': actor['instruction'], 'input': dump(body)}
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
        receipt_id, version, wire, receipt_sha256 = proof
        ack = {**ref, 'receipt_id': receipt_id, 'receipt_sha256': receipt_sha256, 'actual_member_id': intent['member'],
               'actual_role_version': version}
        try:
            if intent['op'] == 'repair':
                payload = load(wire['text'])
                artifact = self.h.publish(self.producer, self.slot, payload,
                                          base_revision=intent['base_revision'], request_id=intent['request_id'])
                ack.update(state='published', request_id=intent['request_id'],
                           base_revision=intent['base_revision'], artifact=artifact)
            else:
                state = self.h.state(self.consumer)
                if state_of(state) == 'completed':
                    if state.get('receipt_id', receipt_id) != receipt_id:
                        raise ValueError('completion receipt mismatch')
                elif state_of(self.h.finish(self.consumer, receipt_id)) != 'completed':
                    raise ValueError('handoff store did not complete consumer')
                ack['state'] = 'completed'
        except (ValueError, TypeError, LookupError, RuntimeError, sqlite3.Error):
            return {'state': 'uncertain', 'reason': 'verified receipt could not be applied', **ref}
        # A crash before this commit leaves a reconcilable intent. Publication
        # replays exactly; a completed consume is not finished a second time.
        with self.db:
            self.db.execute('UPDATE jobs SET ack=? WHERE task_id=? AND ack IS NULL', (dump(ack), task_id))
        return ack


def invoke(args, doc):
    plan = parse(doc, args.backend)
    encoded = dump(plan)
    directory = Path(args.state).resolve()
    binary = Path(args.runtime).resolve(strict=True)
    status_only = doc['op'] == 'status'
    db_path = directory / 'runner.sqlite3'
    handoffs_path = directory / 'handoffs.sqlite3'
    if status_only:
        if not db_path.is_file() or not handoffs_path.is_file():
            raise ValueError('no existing handoff state')
    else:
        directory.mkdir(parents=True, exist_ok=True)
    actor = plan['consumer' if doc['op'] == 'consume' else 'producer']
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
        if row is not None and row[1] != encoded:
            raise ValueError('plan differs from the immutable manifest')
        run_id = ident(load_identity(directory)['run_id'], 'runtime run ID')
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

        def open_handoffs():
            if not status_only:
                return Handoffs(handoffs_path, run_id)

            import threading

            class ReadOnlyHandoffs(Handoffs):
                def __init__(self):
                    # Skip the schema DDL in Handoffs.__init__ for status.
                    self.run_id = run_id
                    self._lock = threading.RLock()
                    self._db = sqlite3.connect(
                        handoffs_path.as_uri() + '?mode=ro', uri=True,
                        timeout=30, isolation_level=None, check_same_thread=False,
                    )
                    self._db.row_factory = sqlite3.Row

            return ReadOnlyHandoffs()

        try:
            with closing(open_handoffs()) as h:
                runner = Runner(db, h, runtime_for_dispatch, binary, directory, run_id, plan)
                if not status_only:
                    h.bind(runner.consumer, runner.slot, runner.producer, plan['required_fields'])
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

