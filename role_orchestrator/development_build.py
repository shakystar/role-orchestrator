"""Verify the isolated executor's candidate commit against both reviewed diffs."""
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory


def verify_candidate(repo, reviewed, execution):
    commit, tree = execution.get('candidate_commit'), execution.get('candidate_tree')
    if (not all(isinstance(v, str) and re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', v) for v in (commit, tree))
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(execution.get('candidate_build_image')))):
        raise ValueError('candidate commit, tree and executor image are required')
    with TemporaryDirectory() as directory:
        target = Path(directory) / 'verify'
        # All index and object writes stay in a disposable local clone.
        subprocess.run(['git', 'clone', '--local', '--shared', '--no-checkout', '--quiet', str(repo.resolve()),
                        str(target)], check=True, capture_output=True)
        git = ['git', '--no-replace-objects', '-C', str(target)]
        def read(*args):
            return subprocess.check_output([*git, *args], stderr=subprocess.PIPE).decode().strip()
        if read('cat-file', '-t', commit) != 'commit' or read('rev-parse', commit + '^{tree}') != tree:
            raise ValueError('reported candidate tree differs from commit')
        subprocess.run([*git, 'read-tree', execution['base_sha']], check=True, capture_output=True)
        for key in ('regression', 'author'):
            subprocess.run([*git, 'apply', '--cached', '--whitespace=nowarn', '-'],
                           input=reviewed[key]['payload']['diff'].encode(), check=True, capture_output=True)
        if read('write-tree') != tree:
            raise ValueError('candidate tree differs from the reviewed implementation and regression')
