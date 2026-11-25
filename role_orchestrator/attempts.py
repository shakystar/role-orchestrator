"""Version 1 of the public, dispatch-free attempt inspection interface."""
import base64
import hashlib
import re
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


class ConfirmedAssignmentMismatch(ValueError):
    """The observed wire and receipt agree on a different task ID."""


class AttemptReader:
    def __init__(self, binary, state, run_id):
        self.binary = binary
        self.state = state
        self.run_id = run_id

    def read(self, attempt):
        reply = query(self.binary, '--state', str(self.state/'runtime'), '--inspect-attempt',
                      attempt, '--run-id', self.run_id)
        if (type(reply.get('version')) is not int or
                (reply.get('version'), reply.get('run_id'), reply.get('attempt_id')) != (1, self.run_id, attempt)):
            raise ValueError('inspection reply identity mismatch')
        if reply['status'] == 'absent':
            return None
        if reply['status'] != 'observed':
            raise ValueError('unexpected inspection status')
        return (base64.b64decode(reply['wire'], validate=True),
                base64.b64decode(reply['receipt'], validate=True))

    def bound(self, attempt, request, input_sha256=None):
        """Inspect one attempt and bind its native task and wire bytes to caller intent."""
        evidence = self.read(attempt)
        if evidence is None:
            return None
        wire_bytes, receipt_bytes = evidence
        wire, receipt = strict_json(wire_bytes), strict_json(receipt_bytes)
        if not isinstance(wire, dict) or not isinstance(receipt, dict):
            raise ValueError('invalid_receipt_envelope')
        if (type(wire.get('schema_version')) is not int or wire['schema_version'] != 1
                or wire.get('run_id') != self.run_id or wire.get('attempt_id') != attempt
                or any(wire.get(key) != value for key, value in request.items() if key != 'id')
                or not isinstance(wire.get('input'), str)):
            raise ValueError('wire_request_mismatch')
        expected_id, observed_id = request.get('id'), wire.get('id')
        id_mismatch = 'id' in request and observed_id != expected_id
        if id_mismatch and not all(isinstance(value, str) and value.strip()
                                   for value in (observed_id, expected_id)):
            raise ValueError('wire_request_mismatch')
        if input_sha256 is not None and hashlib.sha256(wire['input'].encode()).hexdigest() != input_sha256:
            raise ValueError('wire_input_mismatch')
        task = receipt.get('task')
        if (not isinstance(task, dict) or task.get('version') != 'v1'
                or any(task.get(key) != wire[key] for key in ('id', 'role', 'input'))
                or receipt.get('run_id', self.run_id) != self.run_id
                or receipt.get('attempt_id', attempt) != attempt):
            raise ValueError('receipt_task_mismatch')
        if id_mismatch:
            status = receipt.get('status')
            if not isinstance(status, str) or not status.strip():
                raise ValueError('invalid_receipt_envelope')
            raise ConfirmedAssignmentMismatch('assignment_mismatch')
        return receipt, hashlib.sha256(receipt_bytes).hexdigest()


def terminal_failure(receipt):
    """A bound final failure is actionable; ambiguous execution metadata stays uncertain."""
    if receipt.get('status') != 'failed':
        return None
    result = receipt.get('result')
    failure = result.get('failure') if isinstance(result, dict) else None
    if failure is None:
        if isinstance(receipt.get('error'), str) and receipt['error'].strip():
            return 'runtime_failed'
    elif (isinstance(failure, dict) and type(failure.get('version')) is int and failure['version'] == 1
          and failure.get('execution') in ('completed', 'not_started')
          and isinstance(failure.get('code'), str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', failure['code'])):
        return failure['code']
    raise ValueError('failure_receipt_inconclusive')
