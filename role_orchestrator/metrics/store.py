import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime

KINDS = {'task.accepted', 'task.waiting', 'task.ready', 'attempt.started',
         'attempt.finished', 'startup.retry', 'result.cache_hit'}
FIELDS = {'schema_version','event_id','run_id','task_id','attempt_id','source',
          'revision','kind','occurred_at','payload'}
IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)

def validate(event):
    if not isinstance(event, dict) or set(event)-FIELDS-{'trace_id','span_id'} or not FIELDS<=set(event):
        raise ValueError('invalid event fields')
    if type(event['schema_version']) is not int or event['schema_version']!=1 or event['source']!='runtime':
        raise ValueError('unsupported event version/source')
    for key in ('event_id','run_id','task_id','attempt_id'):
        if not isinstance(event[key],str) or not IDENT.fullmatch(event[key]):
            raise ValueError('invalid event identity')
    if type(event['revision']) is not int or event['revision']<1 or event['kind'] not in KINDS:
        raise ValueError('invalid event revision/kind')
    if not isinstance(event['payload'],dict) or len(canonical(event).encode())>16*1024:
        raise ValueError('invalid payload')
    timestamp = datetime.fromisoformat(event['occurred_at'].replace('Z','+00:00'))
    if timestamp.utcoffset() is None:
        raise ValueError('timestamp requires timezone')
    p=event['payload']
    expected={'task.accepted':'ready','task.waiting':'waiting','task.ready':'ready','attempt.started':'running','startup.retry':'running','result.cache_hit':'completed'}
    if event['kind'] in expected and p.get('status')!=expected[event['kind']]:raise ValueError('kind/status mismatch')
    if event['kind']=='attempt.finished':
        if p.get('outcome') not in {'completed','waiting','uncertain','failed','canceled'}:
            raise ValueError('invalid terminal outcome')
        if p.get('status')!=p['outcome']:raise ValueError('outcome/status mismatch')
        if p.get('execution') not in {'not_started','unknown','started','completed'}:
            raise ValueError('invalid execution evidence')
        if p.get('duration_ns') is not None and (type(p['duration_ns']) is not int or p['duration_ns']<0):
            raise ValueError('invalid duration')
    if event['kind']=='startup.retry' and (type(p.get('retry_index')) is not int or p['retry_index']<1):
        raise ValueError('invalid retry index')
    if event['kind']=='result.cache_hit' and not isinstance(p.get('delivery_id'),str):
        raise ValueError('missing delivery identity')

class Metrics:
    def __init__(self, path, run_id, max_bytes=64*1024*1024):
        self.run_id, self.max_bytes = run_id, max_bytes
        self.db=sqlite3.connect(path)
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS binding(id INTEGER PRIMARY KEY CHECK(id=1),run_id TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(source TEXT,run_id TEXT,event_id TEXT,body TEXT NOT NULL,
            PRIMARY KEY(source,run_id,event_id));
          CREATE TABLE IF NOT EXISTS conflicts(event_id TEXT,body TEXT,PRIMARY KEY(event_id,body));
          CREATE TABLE IF NOT EXISTS cursor(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT);
          CREATE TABLE IF NOT EXISTS projection(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL);
        ''')
        try:
            self.db.execute('BEGIN IMMEDIATE')
            rows=self.db.execute('SELECT run_id FROM binding').fetchall()
            if rows and rows!=[(run_id,)]:raise ValueError('metrics run binding conflict')
            self.db.execute('INSERT OR IGNORE INTO binding VALUES(1,?)',(run_id,))
            self.db.commit()
        except Exception:
            self.db.rollback();self.db.close();raise

    def close(self):self.db.close()

    @property
    def cursor(self):
        row=self.db.execute('SELECT value FROM cursor WHERE id=1').fetchone()
        return row[0] if row else None

    def ingest(self, events, next_cursor=None, before_commit=None):
        if len(events)>100:
            raise ValueError('page exceeds limit')
        for event in events:
            validate(event)
            if event['run_id']!=self.run_id:raise ValueError('event run mismatch')
        with self.db:
            used=self.db.execute('SELECT COALESCE(SUM(length(body)),0) FROM events').fetchone()[0]
            used+=self.db.execute('SELECT COALESCE(SUM(length(body)),0) FROM conflicts').fetchone()[0]
            for event in events:
                body=canonical(event)
                key=(event['source'],event['run_id'],event['event_id'])
                prior=self.db.execute('SELECT body FROM events WHERE source=? AND run_id=? AND event_id=?',key).fetchone()
                if prior and prior[0]==body:continue
                if prior and self.db.execute('SELECT 1 FROM conflicts WHERE event_id=? AND body=?',(event['event_id'],body)).fetchone():continue
                if used+len(body.encode())>self.max_bytes:raise ValueError('metrics capacity')
                if prior:
                    self.db.execute('INSERT OR IGNORE INTO conflicts VALUES(?,?)',(event['event_id'],body))
                else:self.db.execute('INSERT INTO events VALUES(?,?,?,?)',(*key,body))
                used+=len(body.encode())
            result=self._summarize()
            self.db.execute('INSERT OR REPLACE INTO projection VALUES(1,?)',(canonical(result),))
            if next_cursor is not None:self.db.execute('INSERT OR REPLACE INTO cursor VALUES(1,?)',(next_cursor,))
            if before_commit:before_commit()
        return result

    def sync(self, client):
        page=client.call('events.list',after_cursor=self.cursor,limit=100)
        events=[row['data'] for row in page['items'] if row['kind']=='event']
        return self.ingest(events,page['next_cursor'])

    def summary(self):
        row=self.db.execute('SELECT body FROM projection WHERE id=1').fetchone()
        return json.loads(row[0]) if row else self._summarize()

    def _summarize(self):
        tasks=defaultdict(list);attempts=set();retries=set();cache=set();terminals=defaultdict(list)
        for body, in self.db.execute('SELECT body FROM events'):
            e=json.loads(body);tasks[e['task_id']].append(e)
            a=(e['task_id'],e['attempt_id'])
            if e['kind']=='attempt.started':attempts.add(a)
            if e['kind']=='attempt.finished':terminals[a].append(e)
            if e['kind']=='startup.retry':retries.add((*a,e['payload']['retry_index']))
            if e['kind']=='result.cache_hit':cache.add(e['payload']['delivery_id'])
        issues=set();statuses={};duration=0;measured=0;wait_duration=0;wait_measured=0;wait_expected=0
        for task,events in tasks.items():
            revs=[e['revision'] for e in events]
            if len(set(revs))!=len(revs):issues.add('revision_conflict')
            if sorted(set(revs))!=list(range(1,len(set(revs))+1)):issues.add('revision_gap')
            state_events=[e for e in events if e['kind'] not in {'startup.retry','result.cache_hit'}]
            latest=max(state_events,key=lambda e:e['revision']) if state_events else None
            statuses[task]=latest['payload'].get('status','unknown') if latest else 'unknown'
            if any(e['payload'].get('history_complete') is False for e in events):issues.add('legacy_history')
            for e in events:
                if e['kind'] in {'attempt.started','task.ready'}:
                    wait_expected+=1
                    p=e['payload']
                    if type(p.get('wait_duration_ns')) is int and p['wait_duration_ns']>=0 and p.get('wait_duration_complete') is True:
                        wait_duration+=p['wait_duration_ns'];wait_measured+=1
                    else:issues.add('waiting_duration_missing')
        for a,ends in terminals.items():
            if a not in attempts:issues.add('missing_attempt_start')
            contents={canonical(e['payload']) for e in ends}
            if len(contents)>1:
                issues.add('terminal_conflict');statuses[a[0]]='conflict';continue
            p=ends[0]['payload']
            if p.get('duration_ns') is not None and p.get('duration_complete') is True:
                duration+=p['duration_ns'];measured+=1
            else:issues.add('duration_missing')
        conflicts=self.db.execute('SELECT COUNT(*) FROM conflicts').fetchone()[0]
        if conflicts:issues.add('event_conflict')
        return {'coverage':'observed_events_only','logical_tasks':len(tasks),'execution_attempts':len(attempts),
                'completed_tasks':sum(v=='completed' for v in statuses.values()),
                'failed_tasks':sum(v in {'failed','uncertain','canceled'} for v in statuses.values()),
                'startup_retries':len(retries),'cache_deliveries':len(cache),
                'execution_duration_ns':duration if measured else None,
                'duration_measured_attempts':measured,'waiting_duration_ns':wait_duration if wait_measured else None,
                'waiting_duration_scope':'runtime_queue_and_suspended_segments',
                'waiting_duration_complete':wait_measured==wait_expected,
                'graph_wait_duration_ns':None, 'graph_wait_duration_reason':'not_recorded',
                'statuses':dict(sorted(statuses.items())), 'complete':not issues,
                'issues':sorted(issues),'conflicts':conflicts,
                'usage':None,'cost':None,'usage_reason':'per_attempt_usage_not_recorded',
                'cost_reason':'usage_and_price_basis_unavailable'}
