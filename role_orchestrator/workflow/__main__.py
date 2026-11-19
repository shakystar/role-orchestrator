import argparse
import json
import os
from pathlib import Path
from .client import Client
from .runner import Workflow

def main():
    p=argparse.ArgumentParser();p.add_argument('--state',type=Path,required=True);p.add_argument('--socket',required=True)
    p.add_argument('--run-id',required=True);p.add_argument('--config',type=Path,required=True)
    p.add_argument('--fixture-crash',choices=['inbox_before_cursor','receipt_before_checkpoint','checkpoint_before_ack'])
    a=p.parse_args();config=json.loads(a.config.read_text())
    workflow=Workflow(a.state,Client(a.socket,a.run_id),config['dependency'],config['consumer_task'],config.get('consumer_id','graph'))
    def crash(point):
        if point==a.fixture_crash:os._exit(17)
    try:print(json.dumps(workflow.poll(crash)))
    finally:workflow.close()
if __name__=='__main__':main()
