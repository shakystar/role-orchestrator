"""Bridge v1 validation; independent of LangGraph and telemetry exporters."""
import hashlib
import json
import math
import re

IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
TRACEPARENT = re.compile(r'00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}\Z')
REQUEST_FIELDS = {'schema_version', 'run_id', 'attempt_id', 'traceparent', 'id', 'role', 'instruction', 'input'}
STAT_FIELDS = {'submitted', 'succeeded', 'failed', 'rejected', 'maxActive', 'maxQueued', 'workers'}
MODEL_NAMES = {'Sol': 'gpt-6.2-sol', 'Astra': 'gpt-6.2-astra'}


def supported_route(model, effort):
    if model == 'gpt-6-luna':
        return effort == 'high'
    return model in MODEL_NAMES.values() and effort in ('low', 'medium', 'high')


def attempt_id(run_id, task_id):
    return 'a-' + hashlib.sha256(f'{run_id}:{task_id}'.encode()).hexdigest()[:32]


def validate_request(request):
    if set(request) != REQUEST_FIELDS or type(request['schema_version']) is not int or request['schema_version'] != 1:
        raise ValueError('bridge v1 fields/version required')
    for field in REQUEST_FIELDS - {'schema_version'}:
        if not isinstance(request[field], str):
            raise ValueError(f'{field} must be string')
    for field in ('run_id', 'attempt_id', 'id', 'role'):
        if not IDENTIFIER.fullmatch(request[field]):
            raise ValueError(f'invalid {field}')
    for field, limit in [('instruction', 16*1024), ('input', 64*1024)]:
        if not 0 < len(request[field].encode()) <= limit:
            raise ValueError(f'invalid {field} length')
    match = TRACEPARENT.fullmatch(request['traceparent'])
    if not match or int(match[1], 16) == 0 or int(match[2], 16) == 0:
        raise ValueError('invalid traceparent')


def validate_response(result, request):
    required = {'schema_version', 'run_id', 'attempt_id', 'id', 'status', 'cached', 'stats'}
    if not isinstance(result, dict) or not required <= set(result) or set(result) - required - {'text', 'error', 'error_code'}:
        raise ValueError('invalid response fields')
    if type(result['schema_version']) is not int or result['schema_version'] != 1:
        raise ValueError('invalid response version')
    if any(result[k] != request[k] for k in ('id', 'run_id', 'attempt_id')):
        raise ValueError('response identity mismatch')
    if result['status'] not in {'succeeded', 'error'} or type(result['cached']) is not bool:
        raise ValueError('invalid response status')
    stats = result['stats']
    if not isinstance(stats, dict) or set(stats) != STAT_FIELDS or any(type(v) is not int or v < 0 for v in stats.values()):
        raise ValueError('invalid stats')
    for key in ('text', 'error', 'error_code'):
        if key in result and not isinstance(result[key], str):
            raise ValueError('invalid result text')
    if result['status'] == 'succeeded':
        if 'text' not in result or 'error' in result or 'error_code' in result:
            raise ValueError('inconsistent successful result')
        if result['cached'] and any(stats.values()):
            raise ValueError('cached result must not count a new execution')
        if not result['cached'] and (stats['submitted'] != 1 or stats['succeeded'] != 1 or stats['failed'] or stats['rejected']):
            raise ValueError('successful execution stats inconsistent')
    elif result['cached'] or not result.get('error_code') or not result.get('error'):
        raise ValueError('invalid error result')


def strict_json(text):
    def finite(token):
        value = float(token)
        if not math.isfinite(value):
            raise ValueError('non-finite JSON')
        return value

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON field')
            result[key] = value
        return result
    result = json.loads(text, object_pairs_hook=pairs, parse_float=finite, parse_constant=finite)
    pending = [result]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            if re.search(r'[\ud800-\udfff]', value):
                raise ValueError('unpaired surrogate in JSON string')
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
    return result

class RequestNotStarted(ValueError):
    """Local validation failed before the first byte could be submitted."""


def encode_request(request):
    try:
        validate_request(request)
        encoded = json.dumps(request, ensure_ascii=False)
        if len(encoded.encode()) > 120 * 1024:
            raise ValueError('request exceeds bridge frame limit')
        return encoded
    except ValueError as error:
        raise RequestNotStarted(str(error)) from error


def preflight_request(logical, run_id):
    # Identifiers and traceparent have exactly the same wire length as at dispatch.
    encode_request(dict(logical, schema_version=1, run_id=run_id,
                        attempt_id=attempt_id(run_id, logical['id']),
                        traceparent='00-'+'1'*32+'-'+'1'*16+'-01'))
