"""Publish a private, immutable snapshot for post-run evaluation."""
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory


def export_handoff(state, run_id, artifacts, receipts):
    destination = Path(state) / 'handoff-evidence'
    with TemporaryDirectory(dir=state) as temporary:
        staging = Path(temporary) / 'evidence'
        staging.mkdir(mode=0o700)
        manifest = {'version': 1, 'run_id': run_id, 'tasks': {}}
        for task, filename in [('producer', 'document.json'), ('consumer', 'review.json')]:
            receipt = receipts[task]
            reference = receipt['artifact']
            if (receipt['task']['id'] != task or receipt['status'] != 'completed'
                    or not receipt['artifact_valid'] or reference['id'] != filename):
                raise ValueError(f'invalid artifact receipt for {task}')
            data = (artifacts / filename).read_bytes()
            if sha256(data).hexdigest() != reference['sha256']:
                raise ValueError(f'artifact changed since receipt for {task}')
            (staging / filename).write_bytes(data)
            manifest['tasks'][task] = {'attempt_id': receipt['attempt_id'], 'artifact': reference}
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        try:
            staging.rename(destination)
        except OSError:
            if not destination.is_dir():
                raise
            for file in staging.iterdir():
                if file.read_bytes() != (destination / file.name).read_bytes():
                    raise ValueError('saved handoff evidence conflicts with current receipts')
    return destination
