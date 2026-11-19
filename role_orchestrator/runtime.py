import base64
import json
import os
import subprocess
import uuid
from pathlib import Path

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from .contracts import attempt_id, strict_json, validate_request, validate_response
from .observability import Observability, MetadataExporter, local_exporter


def load_identity(state):
    path = state / 'run-identity.json'
    if not path.exists():
        if (state / 'runtime' / 'tasks').exists() and any((state / 'runtime' / 'tasks').iterdir()):
            raise ValueError('legacy receipts require a new state directory; no automatic migration')
        identity = {'schema_version': 1, 'run_id': 'r-'+uuid.uuid4().hex, 'trace_id': uuid.uuid4().hex}
        with path.open('x') as f:
            json.dump(identity, f)
            f.flush()
            os.fsync(f.fileno())
    identity = strict_json(path.read_text())
    from .contracts import IDENTIFIER
    if (set(identity) != {'schema_version', 'run_id', 'trace_id'}
        or type(identity['schema_version']) is not int or identity['schema_version'] != 1
        or not isinstance(identity['run_id'], str) or not IDENTIFIER.fullmatch(identity['run_id'])
        or not isinstance(identity['trace_id'], str) or len(identity['trace_id']) != 32
        or any(c not in '0123456789abcdef' for c in identity['trace_id']) or int(identity['trace_id'],16) == 0):
        raise ValueError('invalid run identity')
    return identity


class Runtime:
    """Single-owner bridge v1 client. No automatic work retries."""
    def __init__(self, binary: str, state: Path, backend: str, codex: str, telemetry_env=None, exporter=None):
        self.identity = load_identity(state)
        self.closed = False
        self.had_error = False
        env = dict(os.environ)
        # Do not inherit an unrelated telemetry destination into the subprocess.
        env.pop('ROLE_TRACE_ENDPOINT', None)
        env.pop('ROLE_TRACE_AUTH', None)
        if telemetry_env is not None:
            settings = dict(line.split('=',1) for line in Path(telemetry_env).read_text().splitlines() if line and not line.startswith('#'))
            endpoint = settings.get('LANGFUSE_BASE_URL', 'http://localhost:3300')
            public, secret = settings['LANGFUSE_INIT_PROJECT_PUBLIC_KEY'], settings['LANGFUSE_INIT_PROJECT_SECRET_KEY']
            exporter = local_exporter(endpoint, public, secret)
            env['ROLE_TRACE_ENDPOINT'] = endpoint.rstrip('/')+'/api/public/otel/v1/traces'
            env['ROLE_TRACE_AUTH'] = 'Basic '+base64.b64encode(f'{public}:{secret}'.encode()).decode()
        self.observer = Observability(MetadataExporter(state/'python-spans.jsonl', exporter), trace_id=int(self.identity['trace_id'],16))
        self.activation = self.observer.tracer.start_span('orchestrator.activation', context=Context(), attributes={'run_id': self.identity['run_id'], 'backend': backend})
        self.log = (state / 'runtime-status.jsonl').open('a')
        try:
            self.process = subprocess.Popen(
                [binary, '--state', str(state / 'runtime'), '--backend', backend, '--codex', codex],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                text=True, encoding='utf-8', bufsize=1, env=env,
            )
        except Exception:
            self.activation.end()
            self.observer.close()
            self.log.close()
            raise

    def execute(self, request: dict) -> dict:
        if set(request) != {'id', 'role', 'instruction', 'input'}:
            raise ValueError('logical request fields must be id, role, instruction, input')
        with self.observer.tracer.start_as_current_span(
            'orchestrator.rpc.client', context=trace.set_span_in_context(self.activation),
            record_exception=False, set_status_on_exception=False,
        ) as span:
            try:
                carrier = {}
                TraceContextTextMapPropagator().inject(carrier)
                wire = {**request, 'schema_version': 1, 'run_id': self.identity['run_id'],
                        'attempt_id': attempt_id(self.identity['run_id'], request['id']),
                        'traceparent': carrier['traceparent']}
                validate_request(wire)
                span.set_attributes({'run_id': wire['run_id'], 'task_id': wire['id'], 'attempt_id': wire['attempt_id']})
                result = self._exchange(wire)
                span.set_attribute('cached', result['cached'])
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception:
                self.had_error = True
                span.set_status(Status(StatusCode.ERROR, 'request_failed'))
                raise
            finally:
                self.observer.ended += 1

    def _exchange(self, request):
        validate_request(request)
        encoded = json.dumps(request, ensure_ascii=False)
        if len(encoded.encode()) > 120*1024:
            raise ValueError('request exceeds bridge input limit')
        self.process.stdin.write(encoded+'\n')
        self.process.stdin.flush()
        line = self.process.stdout.readline(1024*1024)
        if not line.endswith('\n'):
            raise RuntimeError('bridge closed or returned oversized response; inspect runtime-status.jsonl')
        result = strict_json(line)
        validate_response(result, request)
        if result['status'] != 'succeeded':
            raise RuntimeError(result['error_code'])
        return result

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()
        self.log.close()
        self.activation.set_status(Status(StatusCode.ERROR if self.had_error else StatusCode.OK))
        self.activation.end()
        self.observer.ended += 1
        self.telemetry_result = self.observer.close()
