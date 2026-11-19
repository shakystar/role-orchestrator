"""Small, optional metadata-only OTel boundary for the local feasibility probe."""
import base64
import threading
from contextlib import contextmanager
from urllib.parse import urlparse

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, SpanLimits
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter


class CountedExporter(SpanExporter):
    def __init__(self, delegate):
        self.delegate = delegate
        self.succeeded = self.failed = 0
        self.lock = threading.Lock()

    def export(self, spans):
        try:
            result = self.delegate.export(spans)
        except Exception:
            result = SpanExportResult.FAILURE
        with self.lock:
            if result == SpanExportResult.SUCCESS:
                self.succeeded += len(spans)
            else:
                self.failed += len(spans)
        return result

    def shutdown(self):
        self.delegate.shutdown()


class Observability:
    # Pilot constants, not production capacity recommendations.
    def __init__(self, exporter, queue_size=64, batch_size=16):
        self.exporter = CountedExporter(exporter)
        self.ended = 0
        self.provider = TracerProvider(
            resource=Resource.create({'service.name': 'role-orchestrator-local-probe'}),
            span_limits=SpanLimits(max_attributes=16, max_attribute_length=256, max_events=0),
        )
        self.provider.add_span_processor(BatchSpanProcessor(
            self.exporter, max_queue_size=queue_size, max_export_batch_size=batch_size,
            schedule_delay_millis=100, export_timeout_millis=2500,
        ))
        self.tracer = self.provider.get_tracer('role-orchestrator.probe', '0.1.0')

    @contextmanager
    def span(self, name, **metadata):
        allowed = {'run_id', 'task_id', 'role', 'backend', 'cached', 'synthetic'}
        attributes = {f'pilot.{k}': v for k,v in metadata.items() if k in allowed}
        with self.tracer.start_as_current_span(
            name, attributes=attributes, record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            finally:
                self.ended += 1

    def close(self):
        flushed = self.provider.force_flush(timeout_millis=5000)
        self.provider.shutdown()
        return {'ended': self.ended, 'exported': self.exporter.succeeded,
                'failed': self.exporter.failed, 'flush_completed': flushed,
                'unexported': self.ended-self.exporter.succeeded-self.exporter.failed}


def local_exporter(base_url, public_key, secret_key):
    parsed = urlparse(base_url)
    if parsed.scheme != 'http' or parsed.hostname not in {'localhost', '127.0.0.1'} or parsed.username or parsed.password or parsed.path not in {'', '/'}:
        raise ValueError('probe requires an explicit loopback HTTP endpoint')
    token = base64.b64encode(f'{public_key}:{secret_key}'.encode()).decode()
    return OTLPSpanExporter(
        endpoint=base_url.rstrip('/') + '/api/public/otel/v1/traces',
        headers={'Authorization': 'Basic ' + token}, timeout=2, max_request_size=256*1024,
    )
