import argparse
import json
from pathlib import Path
from .store import Metrics
from ..workflow.client import Client

def main():
    p=argparse.ArgumentParser();p.add_argument('--state',type=Path,required=True)
    p.add_argument('--socket',required=True);p.add_argument('--run-id',required=True)
    p.add_argument('--max-pages',type=int,default=10)
    a=p.parse_args()
    if not 1<=a.max_pages<=1000:p.error('max-pages must be 1..1000')
    a.state.parent.mkdir(parents=True,exist_ok=True)
    metrics=Metrics(a.state,a.run_id);client=Client(a.socket,a.run_id)
    try:
        drained=False
        for _ in range(a.max_pages):
            before=metrics.cursor;metrics.sync(client)
            if metrics.cursor==before:drained=True;break
        print(json.dumps({'drained_at_observation':drained,**metrics.summary()},indent=2))
    finally:metrics.close()
if __name__=='__main__':main()
