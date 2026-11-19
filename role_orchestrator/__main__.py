import argparse
import fcntl
import json
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .graph import build
from .runtime import Runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["start", "resume", "status"])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--backend", choices=["fixture", "codex"], default="fixture")
    parser.add_argument("--codex", default="codex")
    args = parser.parse_args()
    state = args.state.resolve()
    state.mkdir(parents=True, exist_ok=True)
    manifest = {"schema": 1, "backend": args.backend, "runtime": str(args.runtime.resolve()), "codex": args.codex}
    with (state / "owner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = state / "manifest.json"
        if args.command == "start":
            with path.open("x") as f:
                json.dump(manifest, f)
        elif json.loads(path.read_text()) != manifest:
            raise ValueError("configuration differs from persisted run")
        runtime = None
        try:
            with SqliteSaver.from_conn_string(str(state / "graph.sqlite")) as saver:
                if args.command != "status":
                    runtime = Runtime(manifest["runtime"], state, args.backend, args.codex)
                graph = build(runtime, saver, args.backend)
                config = {"configurable": {"thread_id": "pilot-v1"}, "recursion_limit": 16}
                if args.command == "start":
                    graph.invoke({"proposal": {}, "review": {}, "revisions": 0, "calls": 0, "events": [], "outcome": ""}, config)
                elif args.command == "resume":
                    snapshot = graph.get_state(config)
                    if not snapshot.next:
                        raise ValueError("no pending work")
                    pending_interrupt = any(t.interrupts for t in snapshot.tasks)
                    graph.invoke(Command(resume=True) if pending_interrupt else None, config)
                snapshot = graph.get_state(config)
                report = {"backend": args.backend, "next": list(snapshot.next), **snapshot.values}
                print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            if runtime:
                runtime.close()


if __name__ == "__main__":
    main()
