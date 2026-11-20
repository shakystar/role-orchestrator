"""Run the optional live calibration through the installed agent-eval CLI."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('work', type=Path, help='fresh private output directory')
    parser.add_argument('--semantic-model', required=True)
    args = parser.parse_args()
    cases = json.loads((ROOT / 'evaluation/grounding/blind-cases.json').read_text())
    case, = [entry for entry in cases['cases'] if entry['id'] == 'g04']
    evidence = args.work.resolve() / 'handoff-evidence'
    evidence.mkdir(parents=True, exist_ok=False)
    manifest = {'version': 1, 'run_id': 'constructed-g04', 'tasks': {}}

    def artifact(task, name, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        (evidence / name).write_bytes(data)
        reference = dict(id=name, version='v1', sha256=sha256(data).hexdigest())
        manifest['tasks'][task] = dict(attempt_id='constructed-' + task, artifact=reference)
        return reference

    document = artifact('producer', 'document.json', {'body': cases['source']})
    artifact('consumer', 'review.json', dict(verdict='accept', reason=case['reason'], source_sha256=document['sha256']))
    (evidence / 'manifest.json').write_text(json.dumps(manifest))
    process = subprocess.run([
        sys.executable, str(ROOT / 'scripts/evaluate_handoff.py'), str(evidence),
        '--semantic-model', args.semantic_model, '--requirements', str(Path(__file__).with_name('weekday-weekend.json'))
    ], stdout=subprocess.PIPE, text=True)
    print(process.stdout, end='')
    if process.returncode != 1:
        raise SystemExit('expected an unmet required item; inspect the saved evaluation before retrying')
    result = json.loads(process.stdout)
    observed = [result['outcome'], result['semantic']['outcome'], result['completeness']['outcome'],
                *[i['outcome'] for i in result['completeness']['items']]]
    if observed != ['met', 'met', 'unmet', 'unmet', 'met']:
        raise SystemExit(f'unexpected calibration result: {observed}')


if __name__ == '__main__':
    main()
