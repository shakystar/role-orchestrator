"""Check the standard unittest runner output for the Python goal regression."""
import re


SUMMARY = re.compile(r'Ran (\d+) tests? in \d+(?:\.\d+)?s')
FAILURES = re.compile(r'FAILED \(failures=(\d+)\)')
VERBOSE_CASE = re.compile(r'(test\S* \([^)]+\)) \.\.\.(?: (?:ok|FAIL))?$')


def observe(path):
    """Read one runner summary and the assertion evidence that preceded it."""
    nonblank_tail = []
    case_names = []
    failures = assertions = 0
    awaiting_assertion = False
    summaries = 0
    with path.open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            line = line.rstrip()
            if not line:
                continue
            nonblank_tail = [*nonblank_tail[-1:], line]
            if SUMMARY.fullmatch(line):
                summaries += 1
            if line.startswith('FAIL: '):
                failures += 1
                awaiting_assertion = True
            elif awaiting_assertion and re.match(r'^(?:builtins\.)?AssertionError(?:$|:)', line):
                assertions += 1
                awaiting_assertion = False
            case = VERBOSE_CASE.fullmatch(line)
            if case:
                case_names.append(case[1])
    if summaries != 1 or len(nonblank_tail) != 2:
        return None
    count = SUMMARY.fullmatch(nonblank_tail[0])
    if not count or int(count[1]) < 1:
        return None
    return int(count[1]), nonblank_tail[1], failures, assertions, sorted(case_names)


def matched_unittest_runs(baseline_path, candidate_path):
    baseline = observe(baseline_path)
    candidate = observe(candidate_path)
    if not baseline or not candidate:
        return False
    count, status, failures, assertions, names = baseline
    failure_summary = FAILURES.fullmatch(status)
    return bool(failure_summary and int(failure_summary[1]) == failures == assertions
                and failures > 0 and candidate == (count, 'OK', 0, 0, names))
