"""排放批争用锁。"""
from __future__ import annotations

import sqlite3
import uuid

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit

router = APIRouter(tags=["locks"])


def session_holder(user: str) -> str:
    """同账号双会话须互斥：持锁人按会话区分，裸用户名只用于显式锁接口。"""
    return f"{user}#req-{uuid.uuid4().hex[:12]}"


def _contention(conn, lock_key: str, holder: str, current_holder: str) -> None:
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用失败", f"持有人={current_holder}", now_iso()),
    )


def require_lock(conn, lock_key: str, holder: str) -> None:
    row = conn.execute(
        "SELECT holder FROM effluent_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row and row["holder"] != holder:
        _contention(conn, lock_key, holder, row["holder"])
        # 败方只记争用痕：痕先落库，再抛 409，避免随业务事务一起回滚
        conn.commit()
        raise HTTPException(409, f"锁被占用：{lock_key} 持有人 {row['holder']}")
    if not row:
        try:
            conn.execute(
                "INSERT INTO effluent_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
                (lock_key, holder, now_iso()),
            )
        except sqlite3.IntegrityError:
            # 并发会话抢先插入同键锁：败方只记争用痕，不催生任何业务行
            current = conn.execute(
                "SELECT holder FROM effluent_locks WHERE lock_key=?", (lock_key,)
            ).fetchone()
            current_holder = current["holder"] if current else "未知"
            _contention(conn, lock_key, holder, current_holder)
            conn.commit()
            raise HTTPException(
                409, f"锁被占用：{lock_key} 持有人 {current_holder}"
            ) from None


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM effluent_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        locks = [dict(r) for r in conn.execute("SELECT * FROM effluent_locks").fetchall()]
        logs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM contention_log ORDER BY id DESC LIMIT 50"
            ).fetchall()
        ]
        return {"locks": locks, "contention": logs}


@router.post("/api/locks/acquire")
def acquire(lock_key: str, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        require_lock(conn, lock_key, user)
        write_audit(conn, user, "lock_acquire", lock_key, {})
        conn.commit()
        return {"ok": True, "lock_key": lock_key, "holder": user}


@router.post("/api/locks/release")
def release_api(lock_key: str, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        release(conn, lock_key, user)
        write_audit(conn, user, "lock_release", lock_key, {})
        conn.commit()
        return {"ok": True}
