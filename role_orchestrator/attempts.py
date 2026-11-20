"""Version 1 of the public, dispatch-free attempt inspection interface."""
import base64
import subprocess

from .contracts import strict_json


def query(binary, *args):
    try:
        reply = subprocess.run([str(binary), *args], capture_output=True, timeout=10, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('runtime-bridge inspection timed out') from exc
    if reply.returncode:
        raise ValueError('runtime-bridge rejected the inspection or capability query')
    return strict_json(reply.stdout.decode())


def require_capabilities(binary):
    caps = query(binary, '--capabilities')
    if any(caps.get(name) != 1 for name in ('bridge', 'inspect_attempt', 'fixture_text')):
        raise ValueError('runtime-bridge lacks required v1 interfaces')


class AttemptReader:
    def __init__(self, binary, state, run_id):
        self.binary = binary
        self.state = state
        self.run_id = run_id

    def read(self, attempt):
        reply = query(self.binary, '--state', str(self.state/'runtime'), '--inspect-attempt',
                      attempt, '--run-id', self.run_id)
        if (reply.get('version'), reply.get('run_id'), reply.get('attempt_id')) != (1, self.run_id, attempt):
            raise ValueError('inspection reply identity mismatch')
        if reply['status'] == 'absent':
            return None
        if reply['status'] != 'observed':
            raise ValueError('unexpected inspection status')
        return (base64.b64decode(reply['wire'], validate=True),
                base64.b64decode(reply['receipt'], validate=True))
