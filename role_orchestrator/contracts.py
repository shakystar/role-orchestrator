"""Bridge v1 validation; independent of LangGraph and telemetry exporters."""
import hashlib
import json
import re

IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
TRACEPARENT = re.compile(r'00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}\Z')
REQUEST_FIELDS = {'schema_version', 'run_id', 'attempt_id', 'traceparent', 'id', 'role', 'instruction', 'input'}
STAT_FIELDS = {'submitted', 'succeeded', 'failed', 'rejected', 'maxActive', 'maxQueued', 'workers'}


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
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON field')
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite JSON')))
