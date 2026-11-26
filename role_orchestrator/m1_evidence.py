"""Local, opt-in validation of synthetic M1 turn observations."""


_NATIVE_USAGE_SCOPE = 'final_matched_tokenUsage.last'
_NATIVE_USAGE_FIELDS = {'input_tokens', 'output_tokens', 'cached_input_tokens'}
_FAILED_BINDINGS = {
    'actual_member_id_missing': 'actor_or_role_revision_mismatch',
    'actual_member_id_mismatch': 'actor_or_role_revision_mismatch',
    'actual_role_version_missing': 'actor_or_role_revision_mismatch',
    'actual_role_version_mismatch': 'actor_or_role_revision_mismatch',
    'actor_or_role_revision_mismatch': 'actor_or_role_revision_mismatch',
    'assignment_mismatch': 'assignment_mismatch',
    'receipt_mismatch': 'invalid_receipt_or_output_binding',
    'receipt_binding_mismatch': 'invalid_receipt_or_output_binding',
    'invalid_receipt_or_output_binding': 'invalid_receipt_or_output_binding',
}


def _label(value):
    return isinstance(value, str) and bool(value.strip())


def _sha256(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in '0123456789abcdef' for c in value))


def verify_m1(observations, *, assignment_id, member_id, role_version):
    """Check observe verdicts against caller-pinned identity for one assignment.

    The caller scopes the batch to one window and retains it across checkpoints.
    One completed turn is authorized. A keyed pending turn can be completed by
    later evidence; refresh an unkeyed observation explicitly before retrying.
    Native usage completeness covers only the final matched tokenUsage.last.
    This function does not authenticate the collector, schedule work or dispatch.
    """
    if not all(_label(value) for value in (assignment_id, member_id, role_version)):
        raise ValueError('missing_expected_m1_identity')
    completed, pending, conflicts, errors = {}, set(), set(), set()
    unkeyed = False
    for obs in observations:
        if not isinstance(obs, dict):
            unkeyed = True
            continue
        if obs.get('status') != 'succeeded':
            if obs.get('status') == 'failed':
                reason = obs.get('reason')
                if isinstance(reason, str) and reason in _FAILED_BINDINGS:
                    errors.add(_FAILED_BINDINGS[reason])
                if 'receipt_sha256' in obs and not _sha256(obs['receipt_sha256']):
                    errors.add('invalid_receipt_or_output_binding')
            unkeyed = True
            continue
        evidence = obs.get('turn_evidence')
        if evidence is None or (isinstance(evidence, dict) and evidence.get('turn') is None):
            if (('actual_member_id' in obs and obs['actual_member_id'] != member_id)
                    or ('actual_role_version' in obs and obs['actual_role_version'] != role_version)):
                errors.add('actor_or_role_revision_mismatch')
            if 'receipt_sha256' in obs and not _sha256(obs['receipt_sha256']):
                errors.add('invalid_receipt_or_output_binding')
            unkeyed = True
            continue
        if not isinstance(evidence, dict) or not isinstance(evidence.get('turn'), dict):
            errors.add('invalid_turn_evidence')
            continue
        turn = evidence['turn']
        if evidence.get('assignment_id') != assignment_id or turn.get('assignment_id') != assignment_id:
            errors.add('assignment_mismatch')
            continue
        if (obs.get('actual_member_id') != member_id
                or obs.get('actual_role_version') != role_version
                or turn.get('role_version') != role_version):
            errors.add('actor_or_role_revision_mismatch')
            continue
        key = turn.get('session'), turn.get('turn_id')
        if not all(_label(value) for value in key):
            unkeyed = True
            continue
        if turn.get('completed') is not True:
            pending.add(key)
            continue
        output = evidence.get('output_sha256')
        if (not _sha256(obs.get('receipt_sha256')) or not _sha256(output)
                or turn.get('output_sha256') != output):
            errors.add('invalid_receipt_or_output_binding')
            continue
        scope = turn.get('usage_scope')
        if scope is not None and scope != _NATIVE_USAGE_SCOPE:
            errors.add('invalid_turn_usage')
            continue
        usage = turn.get('usage')
        if usage is not None and (not isinstance(usage, dict) or any(
                not _label(name) or type(count) is not int or count < 0
                for name, count in usage.items()) or (
                    scope == _NATIVE_USAGE_SCOPE and usage.keys() != _NATIVE_USAGE_FIELDS)):
            errors.add('invalid_turn_usage')
            continue
        if scope is None and usage == {}:
            usage = None
        record = output, scope, None if usage is None else dict(usage)
        previous = completed.get(key)
        if previous is None:
            completed[key] = record
        elif previous[:2] != record[:2] or (
                previous[2] is not None and usage is not None and previous[2] != usage):
            conflicts.add(key)
        elif previous[2] is None and usage is not None:
            completed[key] = record

    if conflicts:
        errors.add('conflicting_turn_evidence')
    if len(completed) > 1:
        errors.add('unauthorized_repeat')
    totals = {}
    scopes = {record[1] for record in completed.values()}
    unknown_usage = any(record[2] is None for record in completed.values())
    if len(scopes) == 1:
        for key in completed.keys() - conflicts:
            usage = completed[key][2]
            if usage is not None:
                for name, count in usage.items():
                    totals[name] = totals.get(name, 0) + count
    if errors:
        status, action, reasons = 'failed', 'reject', sorted(errors)
    elif unkeyed or not completed or pending - completed.keys():
        status, action, reasons = 'uncertain', 'hold', ['incomplete_turn_evidence']
    elif unknown_usage:
        status, action, reasons = 'uncertain', 'hold', ['incomplete_turn_usage']
    else:
        status, action, reasons = 'verified', 'accept', []
    verdict = dict(status=status, action=action, reasons=reasons, turn_count=len(completed),
                   usage=totals, automatic_dispatch=False)
    if _NATIVE_USAGE_SCOPE in scopes or unknown_usage:
        verdict['usage_complete'] = status == 'verified'
    if scopes == {_NATIVE_USAGE_SCOPE}:
        verdict['usage_scope'] = _NATIVE_USAGE_SCOPE
    return verdict
