import argparse
from contextlib import closing
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

from .workflow.client import Client, ControlError


SUCCESS = ('completed', 'succeeded', 'success')
PENDING = ('pending', 'queued', 'running', 'submitted', 'accepted')
# Only errors specified as pre-admission validation outcomes are definitive.
REJECTED = ('conflict', 'unsupported')
SHA256 = re.compile(r'[0-9a-f]{64}')
ERROR_CODE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}')
TABLE = 'approval_intents_v1'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS approval_intents_v1 (
    run_id TEXT NOT NULL,
    review_task_id TEXT NOT NULL,
    task_id TEXT NOT NULL UNIQUE,
    artifact_sha256 TEXT NOT NULL,
    project TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'intent',
    reason TEXT,
    PRIMARY KEY (run_id, review_task_id),
    CHECK ((phase IN ('intent', 'accepted') AND reason IS NULL)
        OR (phase = 'rejected' AND reason IS NOT NULL
            AND reason IN ('conflict', 'unsupported')))
)
'''


class RPCUnavailable(RuntimeError):
    pass


class InvalidReceipt(ValueError):
    pass


@dataclass(frozen=True)
class Intent:
    artifact_sha256: str
    project: str
    phase: str
    reason: str | None


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def artifact_ref(receipt):
    artifact = receipt.get('artifact')
    if receipt.get('artifact_valid') is not True or not isinstance(artifact, dict):
        raise InvalidReceipt
    ident, version, digest = (artifact.get(k) for k in ('id', 'version', 'sha256'))
    if (not nonempty(ident)
            or not (nonempty(version) or (type(version) is int and version >= 0))
            or not isinstance(digest, str) or not SHA256.fullmatch(digest.lower())):
        raise InvalidReceipt
    return {'id': ident, 'version': version, 'sha256': digest.lower()}


def try_flock(fd, mode):
    try:
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


class Approval:
    def __init__(self, state, client, run_id, review_task_id):
        self.state = Path(state).resolve()
        self.client = client
        self.key = (run_id, review_task_id)
        encoded = json.dumps(list(self.key), ensure_ascii=False, separators=(',', ':'))
        self.task_id = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        self.lock = self.state.with_name(self.state.name + '.locks') / self.task_id

    def result(self, status, **fields):
        return {'task_id': self.task_id, 'status': status, **fields}

    def connect(self):
        # Opening an existing DB in rw mode permits SQLite hot-journal recovery,
        # but cannot create a missing DB. Status never issues a SQL write.
        return sqlite3.connect(self.state.as_uri() + '?mode=rw', timeout=2, uri=True)

    def read_intent(self):
        if not self.state.exists():
            return None
        with closing(self.connect()) as db:
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
            ).fetchone() is None:
                return None
            row = db.execute(
                'SELECT task_id, artifact_sha256, project, phase, reason '
                'FROM approval_intents_v1 WHERE run_id = ? AND review_task_id = ?',
                self.key,
            ).fetchone()
        if row is None:
            return None
        if row[0] != self.task_id or not nonempty(row[2]):
            raise sqlite3.DatabaseError('invalid approval state')
        return Intent(*row[1:])

    def insert_intent(self, digest, project):
        fd = os.open(self.state, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with closing(self.connect()) as db, db:
            db.execute('PRAGMA synchronous = FULL')
            db.execute('BEGIN IMMEDIATE')
            db.execute(SCHEMA)
            inserted = db.execute(
                'INSERT INTO approval_intents_v1 '
                '(run_id, review_task_id, task_id, artifact_sha256, project) '
                'VALUES (?, ?, ?, ?, ?) ON CONFLICT(run_id, review_task_id) DO NOTHING',
                (*self.key, self.task_id, digest, project),
            ).rowcount == 1
        # Both context managers have exited: the intent was committed before RPC.
        return inserted

    def set_phase(self, phase, reason=None):
        with closing(self.connect()) as db, db:
            db.execute('PRAGMA synchronous = FULL')
            db.execute(
                'UPDATE approval_intents_v1 SET phase = ?, reason = ? '
                "WHERE run_id = ? AND review_task_id = ? AND phase = 'intent'",
                (phase, reason, *self.key),
            )

    def active(self):
        try:
            fd = os.open(self.lock, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        try:
            return not try_flock(fd, fcntl.LOCK_SH)
        finally:
            os.close(fd)

    def rpc(self, method, **params):
        try:
            return self.client.call(method, **params)
        except ControlError:
            raise
        except Exception:
            # Neither transport exceptions nor malformed frames prove no admission.
            raise RPCUnavailable from None

    def receipt(self, task_id, project=None):
        receipt = self.rpc('receipts.get', task_id=task_id)
        task = receipt.get('task') if isinstance(receipt, dict) else None
        if (not isinstance(task, dict) or task.get('id') != task_id
                or not nonempty(task.get('role')) or not nonempty(task.get('project'))
                or (project is not None and task['project'] != project)
                or not isinstance(receipt.get('status'), str)):
            raise InvalidReceipt
        return receipt

    def submit(self, digest):
        try:
            ack = self.rpc('revisions.approve', review_task_id=self.key[1], artifact_sha256=digest)
        except ControlError as exc:
            if exc.code in REJECTED:
                self.set_phase('rejected', exc.code)
            return
        except RPCUnavailable:
            return
        if (isinstance(ack, dict) and ack.get('task_id') == self.task_id
                and type(ack.get('cached')) is bool and ack.get('status') in PENDING):
            self.set_phase('accepted')
        # Terminal acknowledgements are not receipts. Observe the actual receipt.

    def observe(self, row, digest=None):
        if digest is not None and digest != row.artifact_sha256:
            return self.result('conflict', error_code='conflict')
        if row.phase == 'rejected':
            return self.result(row.reason, error_code=row.reason)
        try:
            receipt = self.receipt(self.task_id, row.project)
            state = receipt['status']
            if state in SUCCESS:
                return self.result('completed', artifact=artifact_ref(receipt))
            if state == 'failed':
                failure = receipt.get('failure')
                code = failure.get('code') if isinstance(failure, dict) else failure
                if not isinstance(code, str) or not ERROR_CODE.fullmatch(code):
                    raise InvalidReceipt
                return self.result('failed', error_code=code)
            if state in PENDING:
                return self.result('pending')
            raise InvalidReceipt
        except InvalidReceipt:
            return self.result('uncertain', error_code='invalid_receipt')
        except RPCUnavailable:
            return self.result('uncertain', error_code='receipt_unavailable')
        except ControlError as exc:
            if exc.code != 'not_found':
                return self.result('uncertain', error_code='receipt_unavailable')
        # The receipt may have been read while submit was still in flight.
        running = self.active()
        latest = self.read_intent() or row
        if latest.phase == 'rejected':
            return self.result(latest.reason, error_code=latest.reason)
        return self.result('pending' if running or latest.phase == 'accepted' else 'uncertain')

    def status(self):
        row = self.read_intent()
        return self.result('unapproved') if row is None else self.observe(row)

    def approve(self, digest):
        row = self.read_intent()
        if row is not None:
            return self.observe(row, digest)
        try:
            review = self.receipt(self.key[1])
            if review['status'] not in SUCCESS:
                raise InvalidReceipt
            reviewed = artifact_ref(review)
        except ControlError as exc:
            code = 'not_found' if exc.code == 'not_found' else 'review_unavailable'
            return self.result('unapproved', error_code=code)
        except RPCUnavailable:
            return self.result('unapproved', error_code='review_unavailable')
        except InvalidReceipt:
            return self.result('unapproved', error_code='invalid_review_receipt')
        if reviewed['sha256'] != digest:
            return self.result('conflict', error_code='conflict')

        self.lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if try_flock(fd, fcntl.LOCK_EX):
                # SQLite uniqueness is checked even when this process owns the lock.
                if self.insert_intent(digest, review['task']['project']):
                    self.submit(digest)
        finally:
            os.close(fd)
        # Lock files are never removed; unlinking one would permit two owners.
        row = self.read_intent()
        return self.result('pending') if row is None else self.observe(row, digest)


class CLIParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, '{"error_code":"invalid_arguments"}\n')


def main(argv=None):
    parser = CLIParser(description='Approve or inspect one saved revision proposal.')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('approve', 'status'):
        p = sub.add_parser(command)
        for flag in ('--socket', '--run-id', '--review-task-id', '--state'):
            p.add_argument(flag, required=True)
        if command == 'approve':
            p.add_argument('--artifact-sha256', required=True, type=str.lower)
    args = parser.parse_args(argv)
    if not all(map(nonempty, (args.socket, args.run_id, args.review_task_id, args.state))):
        parser.error('empty argument')
    if args.command == 'approve' and not SHA256.fullmatch(args.artifact_sha256):
        parser.error('invalid SHA-256')
    try:
        approval = Approval(args.state, Client(args.socket, args.run_id), args.run_id, args.review_task_id)
    except (ValueError, UnicodeError):
        parser.error('invalid argument')
    except (OSError, RuntimeError):
        print(json.dumps({'status': 'uncertain', 'error_code': 'state_unavailable'}))
        return 1
    try:
        result = approval.approve(args.artifact_sha256) if args.command == 'approve' else approval.status()
    except (OSError, sqlite3.Error):
        print(json.dumps(approval.result('uncertain', error_code='state_unavailable')))
        return 1
    print(json.dumps(result, ensure_ascii=True, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    sys.exit(main())

