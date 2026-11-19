import fcntl
import json
import sqlite3
from pathlib import Path
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command, interrupt
from .client import ControlError

class State(TypedDict):
    delivery_id: str
    done: bool

class Workflow:
    """One graph owner. Inbox is a derived delivery log, never completion authority."""
    def __init__(self, path, client, dependency, consumer_task, consumer_id='graph'):
        self.path=Path(path);self.path.mkdir(parents=True,exist_ok=True)
        self.client=client;self.dependency=dependency;self.task=consumer_task;self.consumer_id=consumer_id
        self.lock=(self.path/'workflow.lock').open('a')
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except Exception:self.lock.close();raise
        self.db=sqlite3.connect(self.path/'inbox.sqlite')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS binding(id INTEGER PRIMARY KEY,body TEXT);
          CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY,body TEXT NOT NULL,state TEXT NOT NULL,reason TEXT,checkpoint TEXT);
          CREATE TABLE IF NOT EXISTS cursor(id INTEGER PRIMARY KEY,value TEXT);
        ''')
        binding=json.dumps({'run_id':client.run_id,'dependency':dependency,'task':consumer_task,'consumer':consumer_id},sort_keys=True)
        prior=self.db.execute('SELECT body FROM binding WHERE id=1').fetchone()
        if prior and prior[0]!=binding:
            self.close();raise ValueError('workflow binding conflict')
        with self.db:self.db.execute('INSERT OR IGNORE INTO binding VALUES(1,?)',(binding,))

    def close(self):
        self.db.close();self.lock.close()

    def _fetch(self, crash=None):
        row=self.db.execute('SELECT value FROM cursor WHERE id=1').fetchone()
        page=self.client.call('notifications.list',consumer_id=self.consumer_id,after_cursor=row[0] if row else None,limit=100)
        with self.db:
            used=self.db.execute('SELECT COALESCE(SUM(length(body)),0) FROM inbox').fetchone()[0]
            for item in page['items']:
                body=json.dumps(item,sort_keys=True);key=item['delivery_id']
                prior=self.db.execute('SELECT body FROM inbox WHERE id=?',(key,)).fetchone()
                if prior and prior[0]!=body:raise ValueError('delivery conflict')
                if not prior:
                    used+=len(body.encode())
                    if used>64*1024*1024:raise ValueError('inbox capacity')
                self.db.execute("INSERT OR IGNORE INTO inbox VALUES(?,?,'pending',NULL,NULL)",(key,body))
            if crash:crash('inbox_before_cursor')
            self.db.execute('INSERT OR REPLACE INTO cursor VALUES(1,?)',(page['next_cursor'],))

    def _ready(self, notice):
        d=self.dependency;m=notice['message']
        if (m['from']!=d['sender'] or m['to']!=d['recipient'] or m['correlation']!=d['correlation']
            or notice['producer_task_id']!=d['producer_task_id'] or notice['artifact']['version']!=d['artifact_version']):
            return False,'dependency_mismatch'
        try:receipt=self.client.call('receipts.get',task_id=d['producer_task_id'])
        except ControlError as e:
            if e.code=='not_found':return False,'receipt_missing'
            raise
        if {k:receipt['task'][k] for k in ('project','role','version','instance')}!=d['sender']:
            return False,'producer_identity_mismatch'
        if receipt['status']!='completed':return False,'producer_'+receipt['status']
        if not receipt['artifact_valid'] or receipt['artifact']!=notice['artifact']:
            return False,'artifact_invalid'
        return True,''

    def poll(self, crash=None):
        self._fetch(crash)
        def await_dependency(_):
            notice=interrupt({'waiting_for':self.dependency['correlation']})
            ready,reason=self._ready(notice)
            if not ready:raise RuntimeError(reason)
            return {'delivery_id':notice['delivery_id']}
        def consume(state):
            # Recheck evidence on node replay, not merely on the original wakeup.
            row=self.db.execute('SELECT body FROM inbox WHERE id=?',(state['delivery_id'],)).fetchone()
            ready,reason=self._ready(json.loads(row[0]))
            if not ready:raise RuntimeError(reason)
            result=self.client.call('tasks.submit',consumer_id=self.consumer_id,task=self.task)
            receipt=self.client.call('receipts.get',task_id=self.task['id'])
            if receipt['status']!='completed':
                interrupt({'waiting_for':'consumer_receipt'})
                receipt=self.client.call('receipts.get',task_id=self.task['id'])
                if receipt['status']!='completed':raise RuntimeError('consumer not completed')
            if crash:crash('receipt_before_checkpoint')
            return {'done':True}
        with SqliteSaver.from_conn_string(str(self.path/'graph.sqlite')) as saver:
            graph=StateGraph(State);graph.add_node('dependency',await_dependency);graph.add_node('consume',consume)
            graph.add_edge(START,'dependency');graph.add_edge('dependency','consume');graph.add_edge('consume',END)
            graph=graph.compile(checkpointer=saver)
            config={'configurable':{'thread_id':self.client.run_id},'recursion_limit':8}
            snapshot=graph.get_state(config)
            if not snapshot.values:
                graph.invoke({'delivery_id':'','done':False},config)
            else:
                pending=any(t.interrupts for t in snapshot.tasks)
                if snapshot.next==('dependency',):
                    for key,body in self.db.execute("SELECT id,body FROM inbox WHERE state='pending' ORDER BY id").fetchall():
                        notice=json.loads(body);ready,reason=self._ready(notice)
                        with self.db:self.db.execute('UPDATE inbox SET reason=? WHERE id=?',(reason,key))
                        if ready:
                            graph.invoke(Command(resume=notice) if pending else None,config);break
                elif snapshot.next==('consume',):
                    try:receipt=self.client.call('receipts.get',task_id=self.task['id'])
                    except ControlError as e:
                        if e.code!='not_found':raise
                        receipt={'status':'missing'}
                    if not pending or receipt['status']=='completed':
                        graph.invoke(Command(resume=True) if pending else None,config)
            snapshot=graph.get_state(config)
            if snapshot.values.get('done'):
                if crash:crash('checkpoint_before_ack')
                key=snapshot.values['delivery_id'];checkpoint=snapshot.config['configurable']['checkpoint_id']
                with self.db:self.db.execute("UPDATE inbox SET state='processed',checkpoint=? WHERE id=?",(checkpoint,key))
            # Acknowledgments are retried by ID after checkpoint inspection, even if
            # a previous poll crashed after persisting its inbox transition.
            for key,checkpoint in self.db.execute("SELECT id,checkpoint FROM inbox WHERE state='processed'").fetchall():
                self.client.call('notifications.ack',consumer_id=self.consumer_id,delivery_id=key,checkpoint_id=checkpoint)
                with self.db:self.db.execute("UPDATE inbox SET state='acked' WHERE id=?",(key,))
            return {'done':bool(snapshot.values.get('done')), 'next':list(snapshot.next),
                    'inbox':dict(self.db.execute('SELECT state,COUNT(*) FROM inbox GROUP BY state')),
                    'blocked_reasons':[r[0] for r in self.db.execute("SELECT DISTINCT reason FROM inbox WHERE state='pending' AND reason!=''")]}
