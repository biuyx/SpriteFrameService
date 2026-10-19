"""MCP paid generation approvals and conservative quota accounting.

SQLite transactions serialize reservations across callers. An attempted call is
never refunded automatically: a timeout does not prove the provider did no work.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.config import get_settings
from app.services.job_manager import JobSubmissionError


class GenerationLedger:
    def __init__(self, path: Path | None = None):
        self._path = path

    @property
    def path(self):
        return self._path or get_settings().resolved_data_dir / 'generation.sqlite3'

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS quotas (
                    caller TEXT PRIMARY KEY, calls INTEGER NOT NULL, seconds INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, caller TEXT NOT NULL, plan TEXT NOT NULL,
                    expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, caller TEXT NOT NULL, request_id TEXT NOT NULL,
                    approval TEXT UNIQUE NOT NULL, calls INTEGER NOT NULL,
                    seconds INTEGER NOT NULL, closed INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(caller, request_id));
                CREATE TABLE IF NOT EXISTS attempts (
                    batch TEXT NOT NULL, item INTEGER NOT NULL, seconds INTEGER NOT NULL,
                    status TEXT NOT NULL, result TEXT, attempted_at REAL NOT NULL,
                    PRIMARY KEY(batch, item));
                CREATE TABLE IF NOT EXISTS qualified (
                    recipe TEXT PRIMARY KEY, evidence TEXT NOT NULL, note TEXT NOT NULL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def set_quota(self, caller, calls, seconds):
        if not caller or type(calls) is not int or type(seconds) is not int or min(calls, seconds) < 0:
            raise ValueError('额度必须为非负整数，调用方不能为空')
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO quotas VALUES (?,?,?)', (caller, calls, seconds))

    def approve(self, caller, plan, ttl=3600):
        if not caller or not 1 <= ttl <= 86400:
            raise ValueError('审批有效期须为 1–86400 秒，调用方不能为空')
        if not plan.get('items') or plan.get('blockers'):
            raise JobSubmissionError('计划没有付费工序或仍有阻塞，不能审批')
        approval = uuid.uuid4().hex
        with self.db() as db:
            if plan['mode'] == 'production':
                self._require_qualified(db, plan)
            db.execute('INSERT INTO approvals(id,caller,plan,expires) VALUES (?,?,?,?)',
                       (approval, caller, json.dumps(plan, ensure_ascii=False), time.time() + ttl))
        return approval

    @staticmethod
    def _require_qualified(db, plan):
        for item in plan['items']:
            if not db.execute('SELECT 1 FROM qualified WHERE recipe=?', (item['recipe'],)).fetchone():
                raise JobSubmissionError('量产工艺尚未定型：请先试产、审核结果并由管理员 qualify')

    def approval(self, approval, caller):
        with self.db() as db:
            row = db.execute('SELECT * FROM approvals WHERE id=? AND caller=?', (approval, caller)).fetchone()
            if not row:
                raise JobSubmissionError('找不到属于当前调用方的审批')
            return dict(row) | {'plan': json.loads(row['plan'])}

    def reserve(self, approval, caller, request_id):
        if not request_id.strip() or len(request_id) > 128:
            raise JobSubmissionError('请求编号必须为 1–128 个字符')
        with self.db() as db:
            old = db.execute('SELECT * FROM batches WHERE caller=? AND request_id=?',
                             (caller, request_id)).fetchone()
            if old:
                if old['approval'] != approval:
                    raise JobSubmissionError('请求编号已用于其他审批')
                return dict(old)
            row = db.execute('SELECT * FROM approvals WHERE id=? AND caller=?', (approval, caller)).fetchone()
            if not row or row['revoked'] or row['expires'] < time.time():
                raise JobSubmissionError('审批不存在、已撤销或已过期')
            if db.execute('SELECT 1 FROM batches WHERE approval=?', (approval,)).fetchone():
                raise JobSubmissionError('审批已使用，请查询原批次；重做需要新的审批')
            plan = json.loads(row['plan'])
            if plan['mode'] == 'production':
                self._require_qualified(db, plan)
            calls = len(plan['items'])
            seconds = sum(i['seconds'] for i in plan['items'])
            quota = db.execute('SELECT * FROM quotas WHERE caller=?', (caller,)).fetchone()
            used = db.execute('SELECT COALESCE(SUM(calls),0),COALESCE(SUM(seconds),0) FROM batches WHERE caller=?',
                              (caller,)).fetchone()
            if not quota or used[0] + calls > quota['calls'] or used[1] + seconds > quota['seconds']:
                raise JobSubmissionError('MCP 付费额度不足或未配置，请管理员设置调用次数和视频秒数额度')
            bid = uuid.uuid4().hex
            db.execute('INSERT INTO batches VALUES (?,?,?,?,?,?,0)',
                       (bid, caller, request_id, approval, calls, seconds))
            return dict(db.execute('SELECT * FROM batches WHERE id=?', (bid,)).fetchone())

    def dispatch(self, batch, item, seconds):
        """Write before crossing the paid boundary; never replay even after a crash."""
        with self.db() as db:
            row = db.execute('SELECT closed FROM batches WHERE id=?', (batch,)).fetchone()
            if not row or row['closed']:
                raise JobSubmissionError('批次已结束，禁止再次生成')
            approval = db.execute('SELECT a.revoked,a.expires FROM approvals a JOIN batches b ON b.approval=a.id WHERE b.id=?',
                                  (batch,)).fetchone()
            if approval['revoked'] or approval['expires'] < time.time():
                raise JobSubmissionError('审批已撤销或过期，禁止付费调用')
            plan_row = db.execute('SELECT a.plan FROM approvals a JOIN batches b ON b.approval=a.id WHERE b.id=?',
                                  (batch,)).fetchone()
            plan = json.loads(plan_row['plan'])
            if plan['mode'] == 'production':
                self._require_qualified(db, plan)
            try:
                db.execute('INSERT INTO attempts VALUES (?,?,?,\'unknown\',NULL,?)', (batch, item, seconds, time.time()))
            except sqlite3.IntegrityError as exc:
                raise JobSubmissionError('该工序已尝试过付费调用，禁止重放；请检查产物或远端任务') from exc

    def complete(self, batch, item, result):
        with self.db() as db:
            db.execute("UPDATE attempts SET status='succeeded',result=? WHERE batch=? AND item=?",
                       (json.dumps(result, ensure_ascii=False), batch, item))

    def remote(self, batch, item, remote_id):
        with self.db() as db:
            db.execute('UPDATE attempts SET result=? WHERE batch=? AND item=?',
                       (json.dumps({'remote_task_id': remote_id}), batch, item))

    def close(self, batch):
        """Release only never-dispatched reservations. Interrupted calls stay charged."""
        with self.db() as db:
            used = db.execute('SELECT COUNT(*),COALESCE(SUM(seconds),0) FROM attempts WHERE batch=?', (batch,)).fetchone()
            db.execute('UPDATE batches SET calls=?,seconds=?,closed=1 WHERE id=?', (*used, batch))

    def qualify(self, batch, note):
        if not note.strip():
            raise ValueError('定型需要人工审核说明')
        with self.db() as db:
            row = db.execute('SELECT a.plan,b.closed FROM batches b JOIN approvals a ON a.id=b.approval WHERE b.id=?',
                             (batch,)).fetchone()
            if not row:
                raise JobSubmissionError('试产批次不存在')
            plan = json.loads(row['plan'])
            successful = db.execute("SELECT COUNT(*) FROM attempts WHERE batch=? AND status='succeeded'", (batch,)).fetchone()[0]
            if plan['mode'] != 'trial' or not row['closed'] or successful != len(plan['items']):
                raise JobSubmissionError('只能定型已全部成功的试产批次')
            for item in plan['items']:
                db.execute('INSERT OR REPLACE INTO qualified VALUES (?,?,?)', (item['recipe'], batch, note))

    def revoke(self, approval):
        with self.db() as db:
            db.execute('UPDATE approvals SET revoked=1 WHERE id=?', (approval,))

    def is_qualified(self, recipe):
        """Planning is read-only, including when the ledger doesn't exist yet."""
        if not self.path.is_file():
            return False
        db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
        try:
            return bool(db.execute('SELECT 1 FROM qualified WHERE recipe=?', (recipe,)).fetchone())
        finally:
            db.close()

    def mark_draft(self, recipe):
        with self.db() as db:
            db.execute('DELETE FROM qualified WHERE recipe=?', (recipe,))

    def report(self, caller):
        with self.db() as db:
            quota = db.execute('SELECT calls,seconds FROM quotas WHERE caller=?', (caller,)).fetchone()
            batches = [dict(r) for r in db.execute('SELECT * FROM batches WHERE caller=?', (caller,))]
            attempts = [dict(r) for r in db.execute('SELECT a.* FROM attempts a JOIN batches b ON b.id=a.batch WHERE b.caller=?', (caller,))]
            for row in attempts:
                row['result'] = json.loads(row['result']) if row['result'] else None
            return {'quota': dict(quota) if quota else {'calls': 0, 'seconds': 0},
                    'reserved_or_charged': {'calls': sum(b['calls'] for b in batches),
                                           'seconds': sum(b['seconds'] for b in batches)},
                    'batches': batches, 'attempts': attempts,
                    'currency_cost': None,
                    'cost_note': '额度为调用次数和视频秒数；失败或超时按已尝试计入。金额以供应商账单为准。'}


generation_ledger = GenerationLedger()
