"""Executable development role contracts. Persist the selected contract with each plan."""
import hashlib
import json
import re


# Field names are the wire representation of the lab's pinned roster and handoff table.
SOURCE = 'agent-collaboration-lab@0bbd7b12057aef2777ff12773821d0ff0775911a/docs/features/development-handoffs.md'
MODEL_PATCH = {'base_sha': 'string', 'diff': 'string', 'tests': 'array', 'unverified': 'array'}
PATCH_INPUT = {'goal': 'string', 'base_sha': 'string', 'source': 'object', 'regression': 'object',
               'acceptance': 'object', 'output_contract': 'object'}
ROLE_SPECS = {
    'acl-001': ('scenario-writer', {'goal': 'object', 'baseline': 'object'}, 'scenario',
                {'criterion_id': 'string', 'initial_state': 'object', 'actions': 'array', 'observations': 'array'}),
    'acl-002': ('identity-designer', {'scenario': 'object', 'source': 'object'}, 'identity',
                {'keys': 'array', 'serialization': 'string', 'examples': 'array', 'compatibility': 'string'}),
    'acl-003': ('crash-window-analyst', {'identity': 'object', 'source': 'object'}, 'windows',
                {'windows': 'array', 'source_refs': 'array'}),
    'acl-004': ('fixture-author-v2', {'scenario': 'object', 'identity': 'object', 'windows': 'object'},
                'vectors', {'criterion_id': 'string', 'initial_state': 'object', 'actions': 'array',
                            'expected_state': 'string', 'expected_new_dispatches': 'integer'}),
    'acl-005': ('oracle-reviewer-v2', {'goal': 'object', 'scenario': 'object', 'vectors': 'object',
                'windows': 'object'}, 'oracle', {'cases': 'array', 'counterexamples': 'array'}),
    'acl-006': ('go-receipt-author', {'identity': 'object', 'windows': 'object', 'approved_vectors': 'object',
                'source': 'object'}, 'go-change', {'base_sha': 'string', 'tests': 'array', 'unverified': 'array'}),
    'acl-007': ('python-resume-author', {'identity': 'object', 'windows': 'object', 'approved_vectors': 'object',
                'source': 'object'}, 'python-change', {'base_sha': 'string', 'tests': 'array', 'unverified': 'array'}),
    'acl-008': ('regression-author-v2', {'vectors': 'object', 'oracle': 'object', 'source': 'object'},
                'regression-change', {'base_sha': 'string', 'diff': 'string', 'command': 'string',
                                      'expected_control': 'string', 'expected_fault': 'string'}),
    'acl-009': ('evidence-reader', {'goal': 'object', 'baseline': 'object', 'evidence': 'object'},
                'acceptance-report', {'criteria': 'array', 'evidence_refs': 'array', 'unresolved': 'array'}),
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def role_contract(member, role_id=None, profile='lab-v1'):
    if profile not in ('lab-v1', 'development-patch-v1'):
        raise ValueError('unknown_development_profile')
    if member not in ROLE_SPECS:
        if member.startswith('acl-') or profile != 'lab-v1':
            raise ValueError('unknown_development_member')
        return None
    role, inputs, kind, outputs = ROLE_SPECS[member]
    if role_id is not None and role != role_id:
        definitions = [spec for spec in ROLE_SPECS.values() if spec[0] == role_id]
        if not definitions:
            raise ValueError('unknown_role_contract')
        role, inputs, kind, outputs = definitions[0]
    if profile == 'development-patch-v1':
        if role == 'python-resume-author':
            inputs, outputs = PATCH_INPUT, MODEL_PATCH
        elif role == 'oracle-reviewer-v2':
            inputs = {'criterion.id': 'string', 'base_sha': 'string', 'source': 'object',
                      'regression': 'object', 'patch_sha256': 'string', 'authored_revision': 'object'}
            outputs = {'verdict': 'string', 'criterion_id': 'string', 'patch_sha256': 'string', 'reason': 'string'}
        else:
            raise ValueError('member_not_assigned_to_patch_profile')
    spec = dict(version=1, role=role, member=member, profile=profile, inputs=dict(inputs), kind=kind,
                outputs=dict(outputs), source=SOURCE)
    spec['schema_source'] = 'role_orchestrator.role_contracts/' + profile
    return {**spec, 'sha256': digest(canonical(spec))}


def contract_instruction(instruction, contract):
    if not contract:
        return instruction
    return (instruction + '\nReturn a single JSON object with these required fields and types: '
            + canonical(contract['outputs']).decode() + '. Contract SHA256: ' + contract['sha256'])


def validate_fields(payload, fields):
    from .handoff_policy import field_problems
    if not isinstance(payload, dict):
        raise ValueError('role_payload_object_required')
    errors = field_problems(payload, fields)
    if errors:
        raise ValueError('invalid_role_fields:' + ','.join(e['field_path'] for e in errors))
    for name, kind in fields.items():
        field = payload
        for part in name.split('.'):
            field = field[part]
        if kind == 'string' and not field.strip():
            raise ValueError('empty_role_field:' + name)


def validate_inputs(payload, contract):
    validate_fields(payload, contract['inputs'])
    if contract['profile'] == 'development-patch-v1':
        if not re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', payload['base_sha']):
            raise ValueError('invalid_patch_base')
        if contract['role'] == 'oracle-reviewer-v2':
            revision = payload['authored_revision']
            artifact = revision.get('artifact')
            if (not isinstance(artifact, dict) or not isinstance(artifact.get('diff'), str)
                    or digest(artifact['diff'].encode()) != payload['patch_sha256']
                    or artifact.get('base_sha') != payload['base_sha']):
                raise ValueError('review_patch_input_mismatch')


def output_payload(text, contract, inputs):
    from .contracts import strict_json
    value = strict_json(text)
    validate_output(value, contract, inputs)
    return value


def validate_output(value, contract, inputs):
    validate_fields(value, contract['outputs'])
    if contract['profile'] == 'development-patch-v1':
        if contract['role'] == 'python-resume-author':
            if (value['base_sha'] != inputs.get('base_sha')
                    or not re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', value['base_sha'])):
                raise ValueError('patch_base_mismatch')
        elif (value['verdict'] not in ('accept', 'revise', 'blocked')
                or not re.fullmatch('[a-f0-9]{64}', value['patch_sha256'])
                or value['patch_sha256'] != inputs.get('patch_sha256')
                or value['criterion_id'] != inputs.get('criterion', {}).get('id')):
            raise ValueError('review_target_mismatch')
    elif contract['role'] in ('go-receipt-author', 'python-resume-author'):
        if not any(isinstance(value.get(key), str) and value[key].strip()
                   for key in ('diff', 'existing_evidence')):
            raise ValueError('diff_or_existing_evidence_required')
    canonical(value)  # Reject non-finite numbers anywhere in the artifact.
