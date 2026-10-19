"""任务事务日志。重启只恢复记录，不重放有副作用的任务。"""
from __future__ import annotations
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, "
                       "request_key TEXT UNIQUE, fingerprint TEXT NOT NULL, "
                       "created REAL NOT NULL, payload TEXT NOT NULL)")

    def claim_owner(self):
        """只有服务进程能恢复/调度任务，防止第二个 worker 把运行任务误判中断。"""
        self._owner = self.path.with_suffix('.owner.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self._owner.seek(0)
                if not self._owner.read(1):
                    self._owner.write(b'0')
                    self._owner.flush()
                self._owner.seek(0)
                msvcrt.locking(self._owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._owner.close()
            raise RuntimeError('任务库已有调度进程占用，请保持单个服务 worker') from exc

    def close(self):
        if getattr(self, '_owner', None):
            self._owner.close()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, data: dict) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?) "
                       "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                       (data['id'], data.get('request_key'), data.get('fingerprint', ''),
                        data['created_at'], json.dumps(data, ensure_ascii=False, default=str)))

    def get(self, value: str, by_key: bool = False):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM jobs WHERE " +
                             ("request_key" if by_key else "id") + "=?", (value,)).fetchone()
        return json.loads(row[0]) if row else None

    def latest(self, limit: int) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM jobs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def recover(self) -> None:
        with self.connect() as db:
            for jid, payload in db.execute("SELECT id,payload FROM jobs").fetchall():
                data = json.loads(payload)
                if data['status'] in ('queued', 'running'):
                    data.update(status='error', interrupted=True, finished_at=time.time(),
                                error='服务重启，任务已中断。请先检查现有产物；确认重做时使用新的请求编号。',
                                message='服务重启后恢复的中断记录（不会自动重放）')
                    db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(data, ensure_ascii=False), jid))
