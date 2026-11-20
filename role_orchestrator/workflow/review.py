"""A bounded writer/reviewer workflow with caller-provided evidence persistence."""
from typing import Protocol

from ..contracts import strict_json

STAGES = {'write': 'writer', 'review': 'reviewer', 'revise': 'writer', 'final-review': 'reviewer'}
WRITER = 'Write the requested deliverable from the supplied task. Return its text only. Apply review feedback on revision.'
REVIEWER = ('Review the deliverable against the supplied task and materials. Return only JSON '
            'with verdict (accept, revise, or defer) and a nonempty reason.')


def review_value(value):
    if not isinstance(value, dict) or set(value) != {'verdict', 'reason'}:
        raise ValueError('expected verdict and reason')
    if value['verdict'] not in ('accept', 'revise', 'defer') or not isinstance(value['reason'], str) or not value['reason'].strip():
        raise ValueError('invalid review decision')
    return value


def request(stage, text):
    role = STAGES[stage]
    return dict(id=stage, role=role, instruction=WRITER if role == 'writer' else REVIEWER, input=text)


def initial_payload(plan, stage, task):
    payload = {'task': task}
    if plan['backend'] == 'fixture' and stage in plan['fixture']:
        field = 'fixture_text' if STAGES[stage] == 'writer' else 'fixture'
        payload[field] = plan['fixture'][stage]
    return payload


class InjectedReadFailure(BlockingIOError):
    """A recoverable failure at the configured handoff read."""


class ReviewJournal(Protocol):
    """Callbacks for immutable handoffs, durable stage observations and dispatch."""
    def known(self, stage): ...
    def handoff_failure(self, stage): ...
    def mark_fault(self, stage, document, injection): ...
    def handoff_failed(self, stage, document, injection, error): ...
    def read_text(self, sha): ...
    def save_input(self, payload): ...
    def dispatch(self, stage, payload, before, injection): ...


def run_review(plan, task, journal: ReviewJournal, inspection_only=False, legacy_fixture=False):
    rows, applied = [], {}
    document = review = decision = None
    status = 'completed'
    for stage, role in STAGES.items():
        if stage == 'review' and plan['scenario'] == 'solo':
            break
        if stage == 'revise' and (decision != 'revise' or not plan['max_revisions']):
            break
        previous_failure = journal.handoff_failure(stage)
        if previous_failure:
            rows.append(previous_failure)
            if previous_failure['injection']:
                applied[previous_failure['injection']['id']] = previous_failure['injection']
            status = 'failed'
            break
        if inspection_only and not journal.known(stage):
            status = 'prepared'
            break
        injection = next((i for i in plan['injections'] if i['stage'] == stage), None)
        payload = initial_payload(plan, stage, task)
        if legacy_fixture and 'fixture_text' in payload:
            payload['fixture'] = payload.pop('fixture_text')
        if document:
            fault = injection if injection and injection['kind'] == 'tool_failure' else None
            try:
                try:
                    if fault and journal.mark_fault(stage, document, fault):
                        raise InjectedReadFailure('temporarily unavailable')
                    content = journal.read_text(document)
                except InjectedReadFailure:
                    content = journal.read_text(document)
            except (OSError, ValueError) as error:
                row = journal.handoff_failed(stage, document, injection, error)
                rows.append(row)
                if row['injection']:
                    applied[row['injection']['id']] = row['injection']
                status = 'failed'
                break
            payload['document'] = dict(text=content, sha256=document)
        if stage == 'revise':
            payload['review'] = journal.read_text(review)
        before = journal.save_input(payload)
        if injection and injection['kind'] == 'material':
            payload['additional_material'] = injection['text']
        row = journal.dispatch(stage, payload, before, injection)
        rows.append(row)
        if row['injection']:
            applied[row['injection']['id']] = row['injection']
        if row['status'] != 'completed':
            status = row['status']
            break
        if role == 'writer':
            document = row['output_sha256']
        else:
            review = row['output_sha256']
            raw = journal.read_text(review)
            try:
                decision = review_value(strict_json(raw))['verdict']
            except ValueError:
                decision, status = 'invalid_review', 'failed'
                break
    interventions = [applied.get(i['id'], dict(id=i['id'], status='not_applied',
                     reason='stage_not_reached', count=0)) for i in plan['injections']]
    return dict(status=status, stages=rows, review=decision, document=document, injections=interventions)
