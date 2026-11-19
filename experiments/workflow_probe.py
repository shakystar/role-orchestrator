"""Synthetic feasibility probe using real Go mailbox and SQLite checkpoints.
Not a production event listener or scheduler; every poll is explicitly invoked.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command, interrupt

class State(TypedDict):
    done: bool
    completed_tasks: list[str]

def rpc(binary, state, action):
    return json.loads(subprocess.check_output([str(binary), '--state', str(state/'runtime'), '--action', action], text=True))

def poll(binary, state, crash=False):
    def wait(_):
        interrupt({'waiting_for': 'document-v1'})
        if not rpc(binary, state, 'observe')['ready']:
            raise RuntimeError('completion evidence changed; remain uncommitted')
        return {}
    def consume(_):
        result=rpc(binary,state,'consume')
        if crash:
            os._exit(17)  # Simulate receipt committed, graph checkpoint absent.
        assert result['consumer']=='completed'
        return {'done':True,'completed_tasks':['producer','consumer']}
    with SqliteSaver.from_conn_string(str(state/'graph.sqlite')) as saver:
        builder=StateGraph(State)
        builder.add_node('wait',wait);builder.add_node('consume',consume)
        builder.add_edge(START,'wait');builder.add_edge('wait','consume');builder.add_edge('consume',END)
        graph=builder.compile(checkpointer=saver)
        config={'configurable':{'thread_id':'sms-probe'},'recursion_limit':8}
        snapshot=graph.get_state(config)
        if not snapshot.values:
            graph.invoke({'done':False,'completed_tasks':[]},config)
        else:
            pending=any(t.interrupts for t in snapshot.tasks)
            if pending and rpc(binary,state,'observe')['ready']:
                graph.invoke(Command(resume=True),config)
            elif snapshot.next and not pending:
                graph.invoke(None,config)
        values=graph.get_state(config).values
        return {'done':values['done'],'unique_completed_tasks':len(set(values['completed_tasks'])),
                'usage':None,'cost':None,'measurement_scope':'synthetic checkpoint outcomes only'}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--state',type=Path,required=True);p.add_argument('--runtime',type=Path,required=True);p.add_argument('--crash-after-receipt',action='store_true')
    args=p.parse_args();args.state.mkdir(parents=True,exist_ok=True)
    print(json.dumps(poll(args.runtime.resolve(),args.state.resolve(),args.crash_after_receipt)))
