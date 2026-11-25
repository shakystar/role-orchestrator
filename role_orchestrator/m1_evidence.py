"""Local, opt-in validation of synthetic M1 turn observations."""


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
    This function does not authenticate the collector, schedule work or dispatch.
    """
    if not all(_label(value) for value in (assignment_id, member_id, role_version)):
        raise ValueError('missing_expected_m1_identity')
    completed, pending, conflicts, errors = {}, set(), set(), set()
    unkeyed = False
    for obs in observations:
        if not isinstance(obs, dict) or obs.get('status') != 'succeeded':
            unkeyed = True
            continue
        evidence = obs.get('turn_evidence')
        if evidence is None or (isinstance(evidence, dict) and evidence.get('turn') is None):
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
        usage = turn.get('usage', {})
        if not isinstance(usage, dict) or any(
                not _label(name) or type(count) is not int or count < 0
                for name, count in usage.items()):
            errors.add('invalid_turn_usage')
            continue
        record = output, dict(usage)
        if key in completed and completed[key] != record:
            conflicts.add(key)
        else:
            completed[key] = record

    if conflicts:
        errors.add('conflicting_turn_evidence')
    if len(completed) > 1:
        errors.add('unauthorized_repeat')
    totals = {}
    for key in completed.keys() - conflicts:
        for name, count in completed[key][1].items():
            totals[name] = totals.get(name, 0) + count
    if errors:
        status, action, reasons = 'failed', 'reject', sorted(errors)
    elif unkeyed or not completed or pending - completed.keys():
        status, action, reasons = 'uncertain', 'hold', ['incomplete_turn_evidence']
    else:
        status, action, reasons = 'verified', 'accept', []
    return dict(status=status, action=action, reasons=reasons, turn_count=len(completed),
                usage=totals, automatic_dispatch=False)
