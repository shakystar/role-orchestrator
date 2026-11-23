"""Close a reviewed goal case with evidence from the authorized isolated executor."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from . import assignment
from .development_case import run_case, stage
from .handoff_runner import owner
from .role_contracts import canonical, digest, validate_fields
from .unittest_evidence import matched_unittest_runs


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def log_tail(path):
    with path.open('rb') as stream:
        stream.seek(max(0, stream.seek(0, 2) - 2048))
        return stream.read().decode('utf-8', errors='replace')


def bind_execution(case, reviewed, run, oracle, baseline_log, candidate_log, role_baseline_log, role_candidate_log):
    author = reviewed['author']
    reviewer = reviewed['reviewer']
    decision = reviewer['payload']
    if decision['verdict'] != 'accept':
        raise ValueError('patch review did not accept')
    validate_fields(run, {'command': 'string', 'baseline.exit_code': 'integer',
                          'candidate.exit_code': 'integer'})
    regression = reviewed['regression']
    expected = dict(case_id=case['case_id'], base_sha=author['payload']['base_sha'],
                    patch_sha256=digest(author['payload']['diff'].encode()),
                    author_receipt=author['receipt_sha256'], review_receipt=reviewer['receipt_sha256'],
                    criterion_id=decision['criterion_id'], regression_sha256=file_hash(oracle))
    if (not assignment.matches(run, **expected)
            or run['patch_sha256'] != decision['patch_sha256']
            or run['baseline'].get('log_sha256') != file_hash(baseline_log)
            or run['candidate'].get('log_sha256') != file_hash(candidate_log)
            or not assignment.matches(run.get('role_regression'),
                                      receipt_sha256=regression['receipt_sha256'],
                                      baseline_log_sha256=file_hash(role_baseline_log),
                                      candidate_log_sha256=file_hash(role_candidate_log),
                                      diff_sha256=digest(regression['payload']['diff'].encode()))):
        raise ValueError('executor evidence differs from reviewed case or supplied files')
    logs = {'baseline': baseline_log, 'candidate': candidate_log,
            'role_baseline': role_baseline_log, 'role_candidate': role_candidate_log}
    return dict(execution=run, author_receipt=author['receipt_sha256'],
                review_receipt=reviewer['receipt_sha256'],
                log_tails={key: log_tail(path) for key, path in logs.items()})


def close(args, case):
    # Read back through the public case logic. Inspecting cannot start missing stages.
    inspect = SimpleNamespace(**{**vars(args), 'action': 'status'})
    reviewed = run_case(inspect, case)
    if 'reviewer' not in reviewed or 'author' not in reviewed:
        return dict(state='blocked', reason='reviewed patch unavailable', case_state=reviewed.get('state'))
    run = assignment.decode(args.execution.read_bytes())
    evidence = bind_execution(case, reviewed, run, args.oracle, args.baseline_log, args.candidate_log,
                              args.role_baseline_log, args.role_candidate_log)
    if ((run['baseline']['exit_code'], run['candidate']['exit_code']) != (1, 0)
            or not assignment.matches(run['role_regression'], baseline_exit_code=1, candidate_exit_code=0)):
        return dict(state='blocked', reason='required baseline failure and candidate success absent')
    if not matched_unittest_runs(args.role_baseline_log, args.role_candidate_log):
        return dict(state='blocked', reason='role regression did not show matching nonempty unittest runs '
                    'with baseline assertion failure and candidate success')
    evidence_sha = digest(canonical(evidence))
    criterion = run['criterion_id']
    inputs = dict(goal={'text': case['goal'], 'criterion_id': criterion},
                  baseline={'source_sha': run['base_sha'], **run['baseline']},
                  evidence={**evidence, 'reference': evidence_sha,
                            'instructions': 'Evaluate the supplied executor evidence. Return criteria as '
                            'objects with criterion_id and status (pass, fail or unverified). Include the '
                            'exact reference string in evidence_refs. List any unresolved concerns.'})
    with owner(args.state):
        verdict = stage(args, case, 'evidence', 'acl-009', inputs, profile='lab-v1')
    if verdict['status'] != 'succeeded':
        return dict(state=verdict['status'], stage='evidence', detail=verdict)
    artifact = verdict['artifact']
    summary = artifact['payload']
    criteria = summary['criteria']
    accepted = (len(criteria) == 1 and assignment.matches(criteria[0], criterion_id=criterion, status='pass')
                and evidence_sha in summary['evidence_refs'] and not summary['unresolved'])
    return dict(case_id=case['case_id'], state='accepted' if accepted else 'needs_review',
                evidence=artifact, execution_sha256=digest(canonical(run)), patch_sha256=run['patch_sha256'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'status'))
    for name in ('state', 'runtime', 'lab', 'execution', 'oracle', 'baseline-log', 'candidate-log',
                 'role-baseline-log', 'role-candidate-log'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--backend', choices=('fixture', 'codex'), required=True)
    args = parser.parse_args()
    try:
        case = assignment.decode(assignment.read_input())
        result = close(args, case)
        print(assignment.encode(result))
        return 0 if result['state'] == 'accepted' else 1
    except (assignment.AssignmentError, ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(assignment.encode(dict(state='error', reason=str(exc))), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
