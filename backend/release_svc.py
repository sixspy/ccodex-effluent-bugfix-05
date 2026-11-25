"""准排放裁定放行。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, require_lock, session_holder
from models import ReleaseIn
from permit_window import is_release_blocked
from validators import validate_sample_releasable

router = APIRouter(tags=["release"])


@router.get("/api/releases")
def list_releases(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.*, s.sample_code, o.outfall_code FROM discharge_releases r
               JOIN sample_batches s ON s.id=r.sample_id
               JOIN outfalls o ON o.id=r.outfall_id
               ORDER BY r.id DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/releases")
def create_release(body: ReleaseIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    # 持锁人按会话区分：同账号双会话同口抢准排同样互斥
    holder = session_holder(user)
    sample_lock = f"sample:{body.sample_id}"
    with connect() as conn:
        require_lock(conn, sample_lock, holder)
        permit_lock = None
        try:
            sample = conn.execute(
                "SELECT * FROM sample_batches WHERE id=?", (body.sample_id,)
            ).fetchone()
            if not sample:
                raise HTTPException(404, "采样批次不存在")
            validate_sample_releasable(sample["status"])

            # 同口同许可的准排串行在许可锁上：先查后写期间禁止第二路会话插入
            permit_lock = f"permit:{sample['permit_ref']}"
            require_lock(conn, permit_lock, holder)

            # 临期警告窗与准排写口联查：许可存在之外必须过临期窗口径（五页同源）
            permit = conn.execute(
                "SELECT * FROM discharge_permits WHERE permit_code=?",
                (sample["permit_ref"],),
            ).fetchone()
            if not permit:
                raise HTTPException(400, "排放许可不存在")
            if permit["status"] != "有效":
                raise HTTPException(400, "排放许可未生效")
            if is_release_blocked(permit):
                # 甲路径：整笔回退、拒收正式行；临期偏航拒写留痕（先落痕再抛出）
                write_audit(
                    conn,
                    user,
                    "release_near_expiry_reject",
                    sample["sample_code"],
                    {
                        "permit_code": sample["permit_ref"],
                        "valid_until": permit["valid_until"],
                        "near_expiry_window_days": permit["near_expiry_window_days"],
                        "reason": "许可临期窗内禁止正式准排，待续期后办理",
                    },
                )
                conn.commit()
                raise HTTPException(400, "许可已入临期警告窗，正式准排整笔拒收")

            open_hold = conn.execute(
                "SELECT id FROM exceed_holds WHERE sample_id=? AND status='开放'",
                (body.sample_id,),
            ).fetchone()
            if open_hold:
                raise HTTPException(400, "仍有开放超标挂起，禁止准排")
            existing = conn.execute(
                "SELECT id FROM discharge_releases WHERE sample_id=? AND status='已准排'",
                (body.sample_id,),
            ).fetchone()
            if existing:
                raise HTTPException(400, "已准排，勿重复")
            ref = f"RL-{sample['sample_code']}"
            conn.execute(
                """INSERT INTO discharge_releases(sample_id,outfall_id,release_ref,status,officer,released_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    body.sample_id,
                    sample["outfall_id"],
                    ref,
                    "已准排",
                    body.officer,
                    now_iso(),
                ),
            )
            conn.execute(
                "UPDATE sample_batches SET status=? WHERE id=?",
                ("已准排", body.sample_id),
            )
            write_audit(
                conn,
                user,
                "release",
                sample["sample_code"],
                {"release_ref": ref, "officer": body.officer},
            )
            conn.commit()
            return {"release_ref": ref, "status": "已准排"}
        finally:
            if permit_lock is not None:
                release(conn, permit_lock, holder)
            release(conn, sample_lock, holder)
            conn.commit()
