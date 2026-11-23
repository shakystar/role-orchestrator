import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _name(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('expected a nonempty identifier')
    return value


_TYPES = {'object': (dict,), 'array': (list,), 'string': (str,),
          'integer': (int,), 'number': (int, float), 'boolean': (bool,),
          'null': (type(None),)}
_ALIASES = {dict: 'object', list: 'array', str: 'string', int: 'integer',
            float: 'number', bool: 'boolean', type(None): 'null'}


class Handoffs:
    def read(self, producer, kind):
        """Return the checked current artifact and payload, or None if absent."""
        with self._tx(write=False):
            row = self._artifact(producer, kind)
            if row is None:
                return None
            reason, payload = self._inspect(row)
            if reason:
                raise ValueError('current handoff artifact is not usable: ' + reason)
            return {'artifact': self._ref(row), 'payload': payload}

    def read_claimed(self, consumer, slot, *, request_id=None):
        """Read an input of the normal intent, or of the indicated repair intent."""
        _name(consumer), _name(slot)
        with self._tx(write=False):
            if request_id is None:
                intent = self._execution(consumer)
            else:
                r = self._by_request(request_id)
                if r is None or r['target'] != consumer:
                    raise ValueError('request does not identify a repair for this producer')
                intent = self._repair(r)
            if intent is None:
                raise ValueError('no claimed intent')
            refs = [v for v in json.loads(intent['input_refs']) if v['slot'] == slot]
            if len(refs) != 1:
                raise ValueError('slot not uniquely present in claimed inputs')
            return self._read_claimed_ref(refs[0], slot)

    def _read_claimed_ref(self, ref, slot):
        if (ref['run_id'] != self.run_id or ref['kind'] != slot or
                type(ref['revision']) is not int or ref['revision'] < 1):
            raise ValueError('invalid claimed input reference')
        row = self._artifact(ref['producer'], slot, ref['revision'])
        if row is None or row['sha256'] != ref['sha256']:
            raise ValueError('claimed input is missing or has changed')
        reason, payload = self._inspect(row)
        if reason not in (None, 'stale_input'):
            raise ValueError('claimed input failed integrity checks')
        return {'artifact': self._ref(row), 'payload': payload}

    def read_repair_inputs(self, producer, slot, request_id):
        """Read the request base and the repair's own claimed inputs in one snapshot."""
        with self._tx(write=False):
            r = self._by_request(request_id)
            if r is None or r['closed'] or (r['target'], r['slot']) != (producer, slot):
                raise ValueError('request does not identify an open repair for this output')
            intent = self._repair(r)
            if intent is None or intent['done']:
                raise ValueError('no pending repair claim')
            row = self._artifact(producer, slot)
            pin = (row['revision'], row['sha256']) if row is not None else (0, None)
            if pin != (r['base'], r['sha256']):
                raise ValueError('repair base has changed')
            current = None
            if row is not None:
                reason, payload = self._inspect(row)
                if reason not in (None, 'stale_input'):
                    raise ValueError('repair base failed integrity checks')
                current = {'artifact': self._ref(row), 'payload': payload}
            refs = json.loads(intent['input_refs'])
            if not isinstance(refs, list):
                raise ValueError('invalid claimed repair inputs')
            inputs = {}
            for ref in refs:
                name = _name(ref['slot'])
                if name in inputs:
                    raise ValueError('duplicate slot in claimed repair inputs')
                inputs[name] = self._read_claimed_ref(ref, name)
            return current, inputs

    """Run-scoped handoffs. The execution caller owns dispatch and receipt verification."""

    def __init__(self, path, run_id):
        self.run_id = _name(run_id)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript('''
        CREATE TABLE IF NOT EXISTS bindings (
          run TEXT, consumer TEXT, slot TEXT, producer TEXT, fields TEXT,
          PRIMARY KEY (run,consumer,slot));
        CREATE TABLE IF NOT EXISTS artifacts (
          run TEXT, producer TEXT, kind TEXT, revision INTEGER, sha256 TEXT,
          payload BLOB, input_refs TEXT, request_id TEXT,
          PRIMARY KEY (run,producer,kind,revision));
        CREATE TABLE IF NOT EXISTS requests (
          id TEXT PRIMARY KEY, run TEXT, consumer TEXT, slot TEXT, target TEXT,
          base INTEGER, sha256 TEXT, condition TEXT, state TEXT, reason TEXT,
          closed INTEGER NOT NULL DEFAULT 0,
          UNIQUE (run,consumer,slot,target,base,condition));
        CREATE INDEX IF NOT EXISTS requests_output ON requests(run,target,slot,base);
        CREATE TABLE IF NOT EXISTS acceptances (
          run TEXT, consumer TEXT, slot TEXT, revision INTEGER, sha256 TEXT,
          PRIMARY KEY (run,consumer,slot,revision));
        CREATE TABLE IF NOT EXISTS repair_intents (
          run TEXT NOT NULL, target TEXT NOT NULL, slot TEXT NOT NULL, base INTEGER NOT NULL,
          claimant TEXT NOT NULL, input_refs TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (run,target,slot,base));
        CREATE TABLE IF NOT EXISTS executions (
          run TEXT, consumer TEXT, input_refs TEXT, receipt TEXT,
          PRIMARY KEY (run,consumer));
        ''')

    def close(self):
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self, write=True):
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            try:
                yield
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def _q(self, sql, *args):
        return self._db.execute(sql, args)

    def bind(self, consumer, slot, producer, required_fields):
        _name(consumer), _name(slot), _name(producer)
        if producer.startswith('external:'):
            _name(producer[9:])
        if isinstance(required_fields, (str, bytes)):
            raise ValueError('required_fields must be field paths or a path-to-type mapping')
        fields = {}
        pairs = required_fields.items() if isinstance(required_fields, dict) else ((p, None) for p in required_fields)
        for path, kind in pairs:
            if not all(p.strip() for p in _name(path).split('.')):
                raise ValueError('invalid field path')
            kind = _ALIASES.get(kind, kind)
            if kind is not None and kind not in _TYPES:
                raise ValueError('unknown JSON field type')
            fields[path] = kind
        key = self.run_id, consumer, slot
        contract = producer, _json(fields)
        with self._tx():
            old = self._q('SELECT producer,fields FROM bindings WHERE run=? AND consumer=? AND slot=?', *key).fetchone()
            if old is None:
                self._q('INSERT INTO bindings VALUES (?,?,?,?,?)', *key, *contract)
            elif tuple(old) != contract:
                raise ValueError('binding cannot be changed')

    def _artifact(self, producer, kind, revision=None):
        sql = 'SELECT * FROM artifacts WHERE run=? AND producer=? AND kind=?'
        key = self.run_id, producer, kind
        if revision is not None:
            return self._q(sql + ' AND revision=?', *key, revision).fetchone()
        return self._q(sql + ' ORDER BY revision DESC LIMIT 1', *key).fetchone()

    @staticmethod
    def _ref(row):
        return {k: row[k] for k in ('producer', 'kind', 'revision', 'sha256')}

    def _inspect(self, row):
        if row is None:
            return None, None
        pending, seen, stale = [row], set(), False
        try:
            while pending:
                a = pending.pop()
                key = a['producer'], a['kind'], a['revision']
                if key in seen:
                    continue
                seen.add(key)
                raw = a['payload']
                value = json.loads(raw)
                if (hashlib.sha256(raw).hexdigest() != a['sha256'] or
                        _json(value).encode('utf-8') != raw):
                    return 'integrity', None
                if a is row:
                    payload = value
                refs = json.loads(a['input_refs'])
                if not isinstance(refs, list):
                    return 'integrity', None
                for ref in refs:
                    if ref['run_id'] != self.run_id or type(ref['revision']) is not int or ref['revision'] < 1:
                        return 'integrity', None
                    producer, kind = _name(ref['producer']), _name(ref['kind'])
                    dep = self._artifact(producer, kind, ref['revision'])
                    if dep is None or dep['sha256'] != ref['sha256']:
                        return 'integrity', None
                    stale |= self._artifact(producer, kind)['revision'] != dep['revision']
                    pending.append(dep)
            return ('stale_input' if stale else None), payload
        except (ValueError, TypeError, KeyError, RecursionError):
            return 'integrity', None

    def _bindings(self, consumer):
        return self._q('SELECT * FROM bindings WHERE run=? AND consumer=? ORDER BY slot', self.run_id, consumer)

    def _execution(self, consumer):
        return self._q('SELECT * FROM executions WHERE run=? AND consumer=?', self.run_id, consumer).fetchone()

    def _by_request(self, request_id):
        return self._q('SELECT * FROM requests WHERE run=? AND id=?', self.run_id, _name(request_id)).fetchone()

    @staticmethod
    def _unit(r):
        return r['run'], r['target'], r['slot'], r['base']

    def _repair(self, r):
        return self._q('SELECT * FROM repair_intents WHERE run=? AND target=? AND slot=? AND base=?',
                       *self._unit(r)).fetchone()

    def _prepare_claim(self, member, task_id, request_id, preparation):
        # Called inside the transaction that INSERTs the claim, never on a
        # duplicate. Keep the original command inputs if the CLI dies before
        # it can materialize the runtime task in its separate job journal.
        if preparation is not None:
            self._q('CREATE TABLE IF NOT EXISTS cli_preparations (run TEXT NOT NULL, member TEXT NOT NULL, '
                    'task_id TEXT NOT NULL, request_id TEXT, draft TEXT NOT NULL, '
                    'PRIMARY KEY(run, member, task_id), UNIQUE(run, member, request_id))')
            self._q('INSERT INTO cli_preparations(run,member,task_id,request_id,draft) VALUES(?,?,?,?,?)',
                    self.run_id, member, task_id, request_id, _json(preparation))

    def prepared_claim(self, member, *, task_id=None, request_id=None):
        if (task_id is None) == (request_id is None):
            raise ValueError('provide one preparation key')
        with self._tx(write=False):
            if self._q("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cli_preparations'").fetchone() is None:
                return None
            key, value = ('request_id', request_id) if task_id is None else ('task_id', task_id)
            row = self._q('SELECT draft FROM cli_preparations WHERE run=? AND member=? AND ' + key + '=?',
                          self.run_id, member, value).fetchone()
            return None if row is None else json.loads(row['draft'])

    def claim_repair(self, request_id, *, preparation=None):
        """Only the transaction creating the intent grants permission to dispatch."""
        with self._tx():
            r = self._by_request(request_id)
            if r is None:
                raise ValueError('unknown repair request')
            unit = self._unit(r)
            task = hashlib.sha256(_json(unit).encode('utf-8')).hexdigest()
            def result(state, **detail):
                return dict(state=state, repair_task_id=task, **detail)
            if r['target'].startswith('external:'):
                return result('blocked', reason='external', resolver=r['target'])
            intent = self._repair(r)
            if intent is not None:
                return result('completed' if intent['done'] else 'uncertain')
            current = self._artifact(r['target'], r['slot'])
            if (r['closed'] or (r['base'], r['sha256']) !=
                    ((current['revision'], current['sha256']) if current else (0, None))):
                return result('blocked', reason='superseded')
            if r['reason'] in ('slot_limit', 'run_limit'):
                binding = self._q('SELECT * FROM bindings WHERE run=? AND consumer=? AND slot=?',
                                  self.run_id, r['consumer'], r['slot']).fetchone()
                self._request(binding, current, json.loads(r['condition']))
                r = self._by_request(request_id)
            if r['state'] != 'needs_revision':
                return result('blocked', reason=r['reason'], resolver=self._detail(r).get('resolver'))
            if self._inspect(current)[0] == 'integrity':
                return result('blocked', reason='integrity', resolver=r['target'])
            status, refs = self._status(r['target'])
            if len(refs) != len(status['slots']):
                return result('blocked', reason='inputs_not_accepted', resolver=r['target'])
            if self._q('SELECT COUNT(*) FROM repair_intents WHERE run=? AND claimant=? AND slot=?',
                       self.run_id, r['consumer'], r['slot']).fetchone()[0] >= 2:
                return result('blocked', reason='slot_limit')
            if self._q('SELECT COUNT(*) FROM repair_intents WHERE run=?', self.run_id).fetchone()[0] >= 6:
                return result('blocked', reason='run_limit')
            self._q('INSERT INTO repair_intents(run,target,slot,base,claimant,input_refs) VALUES (?,?,?,?,?,?)',
                    *unit, r['consumer'], _json(refs))
            self._prepare_claim(r['target'], task, request_id, preparation)
            return result('ready')

    def publish(self, producer, kind, payload, base_revision=0, request_id=None):
        _name(producer), _name(kind)
        if type(base_revision) is not int or base_revision < 0 or base_revision >= 2**63 - 1:
            raise ValueError('invalid base_revision')
        if request_id is not None:
            _name(request_id)
        raw = _json(payload).encode('utf-8')
        sha = hashlib.sha256(raw).hexdigest()
        with self._tx():
            revision = base_revision + 1
            prior = self._artifact(producer, kind, revision)
            if prior is not None and prior['payload'] == raw and prior['request_id'] == request_id:
                if self._inspect(prior)[0] == 'integrity':
                    raise ValueError('artifact integrity failure')
                return self._ref(prior)
            current = self._artifact(producer, kind)
            if (current['revision'] if current else 0) != base_revision:
                raise ValueError('stale base_revision; revalidate the current revision')
            if current is not None and self._inspect(current)[0] == 'integrity':
                raise ValueError('artifact integrity failure')
            if request_id is not None:
                request = self._q('SELECT * FROM requests WHERE run=? AND id=?', self.run_id, request_id).fetchone()
                if (request is None or request['closed'] or
                        (request['state'] != 'needs_revision' and request['reason'] != 'external') or
                        (request['target'], request['slot'], request['base'], request['sha256']) !=
                        (producer, kind, base_revision, current['sha256'] if current else None)):
                    raise ValueError('request does not authorize this revision')
            if request_id is not None and not producer.startswith('external:'):
                intent = self._repair(request)
                if intent is None or intent['done']:
                    raise ValueError('internal repair publication requires its claimed intent')
                refs = intent['input_refs']
                self._q('UPDATE repair_intents SET done=1 WHERE run=? AND target=? AND slot=? AND base=?',
                        *self._unit(request))
            else:
                if self._q('SELECT 1 FROM repair_intents WHERE run=? AND target=? AND slot=? AND base=?',
                           self.run_id, producer, kind, base_revision).fetchone():
                    raise ValueError('use the request_id for this claimed repair')
                execution = self._execution(producer)
                refs = execution['input_refs'] if execution else '[]'
            self._q('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?)', self.run_id, producer, kind,
                    revision, sha, raw, refs, request_id)
            return self._ref(self._artifact(producer, kind, revision))

    def _requests(self, consumer, slot):
        return self._q('SELECT * FROM requests WHERE run=? AND consumer=? AND slot=? AND closed=0 ORDER BY rowid',
                       self.run_id, consumer, slot).fetchall()

    def _detail(self, request):
        r = request
        semantic, criterion, field, observed, required, check = json.loads(r['condition'])
        result = dict(request_id=r['id'], run_id=r['run'], requester_task_id=r['consumer'],
                      slot=r['slot'], target=r['target'], base_revision=r['base'], state=r['state'],
                      criterion_id=criterion, field_path=field, observed=observed,
                      required_change=required, acceptance_check=check,
                      base_ref=dict(producer=r['target'], kind=r['slot'], revision=r['base'],
                                    sha256=r['sha256']) if r['base'] else None)
        if r['target'].startswith('external:'):
            result['external_owner'] = r['target'][9:]
        else:
            result['target_task_id'] = r['target']
        if r['reason']:
            result.update(reason=r['reason'], resolver=r['consumer'] if r['reason'] in ('cycle', 'conflict') else r['target'])
        return result

    def _cycle(self, consumer, producer):
        return self._q('''WITH RECURSIVE reachable(task) AS (
          SELECT ? UNION SELECT r.target FROM reachable JOIN requests r ON r.consumer=reachable.task
          WHERE r.run=? AND r.closed=0 AND r.state='needs_revision' AND r.base=COALESCE((
            SELECT MAX(a.revision) FROM artifacts a WHERE a.run=r.run AND a.producer=r.target AND a.kind=r.slot),0)
        ) SELECT 1 FROM reachable WHERE task=?''', producer, self.run_id, consumer).fetchone() is not None

    def _request(self, binding, artifact, condition):
        consumer, slot, target = (binding[k] for k in ('consumer', 'slot', 'producer'))
        base = artifact['revision'] if artifact else 0
        run, encoded = self.run_id, _json(condition)
        key = run, consumer, slot, target, base, encoded
        old = self._q('SELECT * FROM requests WHERE run=? AND consumer=? AND slot=? AND target=? AND base=? AND condition=?', *key).fetchone()
        if old and (old['closed'] or old['reason'] not in ('slot_limit', 'run_limit')):
            return self._detail(old)
        external = target.startswith('external:')
        reason = None
        if condition[0]:
            peers = self._q('SELECT condition FROM requests WHERE run=? AND target=? AND slot=? AND base=? AND closed=0',
                            run, target, slot, base)
            for peer in peers:
                other = json.loads(peer[0])
                if other[:3] == condition[:3] and other[4:] != condition[4:]:
                    reason = 'conflict'
                    break
        if not reason:
            reason = 'external' if external else 'cycle' if self._cycle(consumer, target) else None
        if old:
            self._q('UPDATE requests SET state=?, reason=? WHERE id=?',
                    'blocked' if reason else 'needs_revision', reason, old['id'])
            return self._detail(self._by_request(old['id']))
        rid = uuid.uuid4().hex
        self._q('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,0)', rid, *key[:5],
                artifact['sha256'] if artifact else None, encoded,
                'blocked' if reason else 'needs_revision', reason)
        return self._detail(self._q('SELECT * FROM requests WHERE id=?', rid).fetchone())

    @staticmethod
    def _field_problem(payload, fields):
        for path, kind in fields.items():
            value = payload
            for part in path.split('.'):
                if isinstance(value, dict) and part in value:
                    value = value[part]
                elif isinstance(value, list) and part.isascii() and part.isdecimal() and len(part) < 20 and int(part) < len(value):
                    value = value[int(part)]
                else:
                    return path, 'missing', kind or 'present'
            if kind and type(value) not in _TYPES[kind]:
                return path, type(value).__name__, kind
        return None

    def validate(self, consumer, slot, criterion='structure', field=None, observed=None, required=None, check=None, *, reviewed_ref=None):
        return self._validate(consumer, slot, criterion, field, observed, required, check, reviewed_ref, False)

    def accept_review(self, consumer, slot, reviewed_ref):
        """Attest that this revision satisfies all earlier semantic feedback for this slot."""
        return self._validate(consumer, slot, 'structure', None, None, None, None, reviewed_ref, True)

    def _validate(self, consumer, slot, criterion, field, observed, required, check, reviewed_ref, accepting):
        _name(consumer), _name(slot), _name(criterion)
        feedback = (field, observed, required, check)
        semantic = criterion != 'structure' or any(v is not None for v in feedback)
        if semantic:
            _name(field), _name(required), _name(check)
            if observed is None or (isinstance(observed, (str, list, dict, tuple)) and not observed):
                raise ValueError('observed is required for semantic feedback')
            if isinstance(observed, str):
                _name(observed)
        with self._tx():
            key = self.run_id, consumer, slot
            b = self._q('SELECT * FROM bindings WHERE run=? AND consumer=? AND slot=?', *key).fetchone()
            if b is None:
                raise ValueError('unknown input slot')
            a = self._artifact(b['producer'], slot)
            if semantic or accepting or reviewed_ref is not None:
                if (not isinstance(reviewed_ref, dict) or type(reviewed_ref.get('revision')) is not int or
                        reviewed_ref['revision'] < 1 or not isinstance(reviewed_ref.get('sha256'), str) or
                        not reviewed_ref['sha256']):
                    raise ValueError('reviewed_ref requires revision and sha256 from the reviewed artifact')
                expected = dict(run_id=self.run_id, producer=b['producer'], kind=slot, slot=slot)
                if (a is None or (reviewed_ref['revision'], reviewed_ref['sha256']) !=
                        (a['revision'], a['sha256']) or
                        any(k in reviewed_ref and reviewed_ref[k] != v for k, v in expected.items())):
                    return dict(state='blocked', reason='stale_review', resolver=consumer)
            reason, payload = self._inspect(a)
            if reason == 'stale_input' and not semantic and not accepting:
                return self._request(b, a, [False, 'structure', '$', 'stale_input',
                                            'rebuild ' + slot, 'output uses current accepted inputs'])
            if reason:
                return dict(state='blocked', reason=reason, resolver=b['producer'])
            if semantic:
                return self._request(b, a, [True, criterion, *feedback])
            if a is None:
                return self._request(b, a, [False, 'structure', '$', 'missing', 'publish ' + slot, 'artifact is stored and valid'])
            problem = self._field_problem(payload, json.loads(b['fields']))
            if problem:
                path, found, expected = problem
                return self._request(b, a, [False, 'structure', path, found, 'provide ' + path, 'field is ' + expected])
            pending = self._requests(consumer, slot)
            open_now = [r for r in pending if r['base'] == a['revision']]
            if open_now:
                return self._detail(next((r for r in open_now if r['state'] == 'blocked'), open_now[0]))
            reviews = [r for r in pending if r['base'] < a['revision'] and json.loads(r['condition'])[0]]
            if reviews and not accepting:
                return dict(state='blocked', reason='review_required', resolver=consumer,
                            artifact=self._ref(a), open_requests=[self._detail(r) for r in reviews])
            self._q('INSERT OR IGNORE INTO acceptances VALUES (?,?,?,?,?)', *key, a['revision'], a['sha256'])
            self._q('UPDATE requests SET closed=1 WHERE run=? AND consumer=? AND slot=? AND base<?', *key, a['revision'])
            return dict(state='accepted', artifact=self._ref(a))

    def _status(self, consumer):
        execution = self._execution(consumer)
        slots, requests, refs = {}, [], []
        for b in self._bindings(consumer):
            slot = b['slot']
            a = self._artifact(b['producer'], slot)
            revision = a['revision'] if a else 0
            pending = self._requests(consumer, slot)
            details = [dict(self._detail(r), current=r['base'] == revision) for r in pending]
            active = [r['state'] for r in details if r['current']]
            reason, _ = self._inspect(a)
            state = 'pending'
            if reason or 'blocked' in active:
                state = 'blocked'
            elif active:
                state = 'needs_revision'
            elif any(r['base'] < revision and json.loads(r['condition'])[0] for r in pending):
                state, reason = 'blocked', 'review_required'
            elif a and self._q('SELECT 1 FROM acceptances WHERE run=? AND consumer=? AND slot=? AND revision=? AND sha256=?',
                               self.run_id, consumer, slot, revision, a['sha256']).fetchone():
                state = 'accepted'
                refs.append(dict(self._ref(a), run_id=self.run_id, slot=slot))
            info = dict(state=state, revision=revision or None, open_requests=details)
            if reason:
                info.update(reason=reason, resolver=consumer if reason == 'review_required' else b['producer'])
            slots[slot] = info
            requests.extend(details)
        state = 'ready' if len(refs) == len(slots) else 'blocked'
        if execution:
            state = 'completed' if execution['receipt'] is not None else 'uncertain'
        return dict(state=state, slots=slots, open_requests=requests,
                    receipt_id=execution['receipt'] if execution else None), refs

    def state(self, consumer):
        _name(consumer)
        with self._tx(write=False):
            return self._status(consumer)[0]

    def claim_ready(self, consumer, *, preparation=None):
        _name(consumer)
        with self._tx():
            status, refs = self._status(consumer)
            if status['state'] == 'ready':
                self._q('INSERT INTO executions VALUES (?,?,?,NULL)', self.run_id, consumer, _json(refs))
                self._prepare_claim(consumer, 'consume', None, preparation)
            return status['state']

    def finish(self, consumer, receipt_id):
        """Record a receipt only from the execution caller after runtime verification."""
        _name(consumer), _name(receipt_id)
        with self._tx():
            execution = self._execution(consumer)
            if execution is None:
                raise ValueError('no execution intent')
            if execution['receipt'] not in (None, receipt_id):
                raise ValueError('a different receipt was already recorded')
            self._q('UPDATE executions SET receipt=? WHERE run=? AND consumer=?', receipt_id, self.run_id, consumer)
            return 'completed'

