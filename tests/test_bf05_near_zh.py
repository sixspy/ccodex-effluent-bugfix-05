"""effluent-bugfix-05 丙红：两路会话同口抢准排必须串行在准排锁上。

口径：
- 同口同许可的两路准排，败方只记 contention_log 争用痕，不落任何准排行；
- 同账号双会话同样互斥（持锁人按会话区分，不按用户名放行）；
- 临期窗内并发准排，正式区零行。

注：FastAPI TestClient 经单 portal 串行执行，无法在进程内制造真库级并发；
真并发用两条独立 sqlite 连接的工作线程直驱锁具（与 uvicorn 多线程同构），
HTTP 层语义用 TestClient 逐请求断言。tests/duel_near_permit.py 提供并发脚本。
"""
from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException

from app import app
from db import connect, init_db, now_iso
from locks import release, require_lock, session_holder

NEAR_PERMIT = "DP-BF05-NEAR-ZH"
HEALTHY_PERMIT = "DP-BF05-HEALTHY-ZH"
NEAR_UNTIL = "2026-12-08"  # 相对当日 2026-11-25 落在 30 天临期窗内


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "zheng.db")
    init_db()
    with connect() as conn:
        east = conn.execute(
            "SELECT id FROM plants WHERE plant_code='PL-EAST-01'"
        ).fetchone()["id"]
        outfall_id = conn.execute(
            "SELECT id FROM outfalls WHERE outfall_code='OF-E1-B'"
        ).fetchone()["id"]
        for code, until in ((NEAR_PERMIT, NEAR_UNTIL), (HEALTHY_PERMIT, "2028-06-30")):
            conn.execute(
                """INSERT INTO discharge_permits(permit_code,plant_id,quota_m3d,cod_cap,nh3n_cap,
                   tp_cap,period_label,status,valid_until,near_expiry_window_days,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    code, east, 50000, 50, 5, 0.5, "2026Q4", "有效",
                    until, 30, "2026-11-01T10:00:00",
                ),
            )
        ids = {}
        for permit, code in (
            (NEAR_PERMIT, "SM-BF05-ZH-N1"),
            (NEAR_PERMIT, "SM-BF05-ZH-N2"),
            (HEALTHY_PERMIT, "SM-BF05-ZH-H1"),
        ):
            cur = conn.execute(
                """INSERT INTO sample_batches(sample_code,outfall_id,permit_ref,metrics,flow_m3h,
                   generation,status,adjudicated_at,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    code, outfall_id, permit,
                    json.dumps({"cod": 25.0, "nh3n": 2.0, "tp": 0.15}),
                    1100, 1, "已裁定", now_iso(), now_iso(),
                ),
            )
            ids[code] = cur.lastrowid
        conn.commit()
    return ids


def _client() -> TestClient:
    c = TestClient(app)
    tok = c.post(
        "/api/login", json={"username": "admin", "password": "admin123"}
    ).json()["token"]
    c.headers.update({"Authorization": "Bearer " + tok})
    return c


def _formal_rows(sample_id: int) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM discharge_releases "
            "WHERE sample_id=? AND status='已准排'",
            (sample_id,),
        ).fetchone()["c"]


def _contention(lock_key: str) -> list[dict]:
    with connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM contention_log WHERE lock_key=?", (lock_key,)
            ).fetchall()
        ]


def test_parallel_connections_same_permit_lock_serializes(seeded):
    """真并发：两条独立连接的工作线程抢同一许可锁，一得一 409，败方仅留痕。"""
    lock_key = f"permit:{HEALTHY_PERMIT}"
    start = threading.Event()
    outcomes: list = []

    def worker(holder: str, delay: float) -> None:
        conn = connect()
        start.wait()
        try:
            threading.Event().wait(delay)
            require_lock(conn, lock_key, holder)
            # 模拟持锁后的业务写并提交（与准排请求同构）
            conn.execute(
                "INSERT INTO audit_trail(actor,action,target,payload,created_at) "
                "VALUES (?,?,?,?,?)",
                (holder, "probe", lock_key, "{}", now_iso()),
            )
            conn.commit()
            outcomes.append((holder, "acquired"))
        except HTTPException as exc:
            outcomes.append((holder, f"409:{exc.status_code}"))
        finally:
            conn.close()

    t1 = threading.Thread(target=worker, args=("sess-A", 0.0))
    t2 = threading.Thread(target=worker, args=("sess-B", 0.15))
    t1.start(); t2.start()
    start.set()
    t1.join(15); t2.join(15)

    acquired = [h for h, r in outcomes if r == "acquired"]
    rejected = [h for h, r in outcomes if r.startswith("409")]
    assert acquired == ["sess-A"]
    assert rejected == ["sess-B"]
    traces = _contention(lock_key)
    assert len(traces) == 1
    assert traces[0]["actor"] == "sess-B"
    assert traces[0]["action"] == "争用失败"
    with connect() as conn:
        release(conn, lock_key, "sess-A")
        conn.commit()


def test_near_expiry_dual_attempts_leave_zero_formal_rows(seeded):
    """两路会话对同口临期许可准排：正式区零行（每路各自被临期窗拒收）。"""
    s1, s2 = seeded["SM-BF05-ZH-N1"], seeded["SM-BF05-ZH-N2"]
    c1, c2 = _client(), _client()
    r1 = c1.post("/api/releases", json={"sample_id": s1, "officer": "丙路甲"})
    r2 = c2.post("/api/releases", json={"sample_id": s2, "officer": "丙路乙"})
    assert r1.status_code == 400 and r2.status_code == 400
    assert "临期" in r1.json()["detail"] and "临期" in r2.json()["detail"]
    assert _formal_rows(s1) == 0
    assert _formal_rows(s2) == 0
    with connect() as conn:
        yaw = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='release_near_expiry_reject'"
        ).fetchone()["c"]
    assert yaw == 2
    # 锁均已释放，不残留
    with connect() as conn:
        left = conn.execute("SELECT COUNT(*) c FROM effluent_locks").fetchone()["c"]
    assert left == 0


def test_same_user_two_sessions_contend_on_sample_lock(seeded):
    # 预置锁行持锁人是裸用户名 admin——修复前准排写口用用户名当持锁人，
    # 同账号第二会话会被当成持锁本人直接放行（200）；修复后持锁人按会话区分，
    # 同账号另一会话必然 409，并只留争用痕。
    sample_id = seeded["SM-BF05-ZH-H1"]
    with connect() as conn:
        conn.execute(
            "INSERT INTO effluent_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
            (f"sample:{sample_id}", "admin", now_iso()),
        )
        conn.commit()
    try:
        c = _client()
        r = c.post(
            "/api/releases", json={"sample_id": sample_id, "officer": "同账号会话乙"}
        )
        assert r.status_code == 409, f"同账号双会话未互斥：{r.status_code} {r.text}"
        traces = _contention(f"sample:{sample_id}")
        assert len(traces) == 1
        assert traces[0]["action"] == "争用失败"
        assert traces[0]["detail"].startswith("持有人=")
        assert _formal_rows(sample_id) == 0
    finally:
        with connect() as conn:
            conn.execute(
                "DELETE FROM effluent_locks WHERE lock_key=?",
                (f"sample:{sample_id}",),
            )
            conn.commit()


def test_session_holder_distinguishes_same_user_sessions():
    h1, h2 = session_holder("admin"), session_holder("admin")
    assert h1 != h2 and h1.startswith("admin#") and h2.startswith("admin#")


def test_lock_loser_leaves_only_contention_trace(seeded):
    sample_id = seeded["SM-BF05-ZH-H1"]
    with connect() as conn:
        require_lock(conn, f"permit:{HEALTHY_PERMIT}", "先发会话持锁")
        conn.commit()
    try:
        c = _client()
        r = c.post(
            "/api/releases", json={"sample_id": sample_id, "officer": "败方会话"}
        )
        assert r.status_code == 409
        assert _formal_rows(sample_id) == 0
        traces = _contention(f"permit:{HEALTHY_PERMIT}")
        assert len(traces) == 1
        assert traces[0]["actor"] != "先发会话持锁"
        with connect() as conn:
            sample_status = conn.execute(
                "SELECT status FROM sample_batches WHERE id=?", (sample_id,)
            ).fetchone()["status"]
        assert sample_status == "已裁定"
    finally:
        with connect() as conn:
            release(conn, f"permit:{HEALTHY_PERMIT}", "先发会话持锁")
            conn.commit()
