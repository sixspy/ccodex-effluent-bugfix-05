"""effluent-bugfix-05 乙红：临期窗内的影子意图不得被对账/证明当正式准排。

口径：临期准排只许影子意图，正式区零改；未原子升正前任何页面不得显示“已准排”。
本测把影子行直接写入正式表的影子状态，验证对账侧只认真式“已准排”行。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db, now_iso
from locks import require_lock, release

NEAR_PERMIT = "DP-BF05-NEAR-YI"
NEAR_UNTIL = "2026-12-05"  # 相对当日 2026-11-25 落在 30 天临期窗内
SAMPLE_CODE = "SM-BF05-YI-001"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "yi.db")
    init_db()
    c = TestClient(app)
    h = _auth(c)
    _make_near_permit_and_shadow(c, h)
    return c, h


def _auth(c: TestClient, user="admin", password="admin123") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _make_near_permit_and_shadow(c: TestClient, h: dict) -> None:
    with connect() as conn:
        plant_id = conn.execute(
            "SELECT id FROM plants WHERE plant_code='PL-WEST-02'"
        ).fetchone()["id"]
        conn.execute(
            """INSERT INTO discharge_permits(permit_code,plant_id,quota_m3d,cod_cap,nh3n_cap,
               tp_cap,period_label,status,valid_until,near_expiry_window_days,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                NEAR_PERMIT, plant_id, 40000, 50, 5, 0.5, "2026Q4", "有效",
                NEAR_UNTIL, 30, "2026-11-01T10:00:00",
            ),
        )
        outfall_id = conn.execute(
            "SELECT id FROM outfalls WHERE outfall_code='OF-W2-A'"
        ).fetchone()["id"]
        cur = conn.execute(
            """INSERT INTO sample_batches(sample_code,outfall_id,permit_ref,metrics,flow_m3h,
               generation,status,adjudicated_at,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                SAMPLE_CODE, outfall_id, NEAR_PERMIT,
                json.dumps({"cod": 30.0, "nh3n": 2.0, "tp": 0.2}),
                1200, 1, "已裁定", now_iso(), now_iso(),
            ),
        )
        sample_id = cur.lastrowid
        # 模拟临期窗内只登记影子意图（待续期后原子升正），正式区不得出现“已准排”
        conn.execute(
            """INSERT INTO discharge_releases(sample_id,outfall_id,release_ref,status,officer,released_at)
               VALUES (?,?,?,?,?,?)""",
            (
                sample_id, outfall_id, f"SHADOW-{SAMPLE_CODE}", "影子意图",
                "乙路值班", now_iso(),
            ),
        )
        conn.commit()


def _sample_id() -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT id FROM sample_batches WHERE sample_code=?", (SAMPLE_CODE,)
        ).fetchone()["id"]


def test_shadow_intent_attempt_does_not_formalize(client):
    c, h, = client
    sample_id = _sample_id()

    # 临期窗内走准排写口：必须拒收正式行，影子不得升正
    r = c.post(
        "/api/releases",
        headers=h,
        json={"sample_id": sample_id, "officer": "乙路准排"},
    )
    assert r.status_code == 400, f"临期窗内影子被升正：{r.status_code} {r.text}"

    with connect() as conn:
        statuses = [
            dict(r)["status"]
            for r in conn.execute(
                "SELECT status FROM discharge_releases WHERE sample_id=?",
                (sample_id,),
            ).fetchall()
        ]
        sample_status = conn.execute(
            "SELECT status FROM sample_batches WHERE id=?", (sample_id,)
        ).fetchone()["status"]
    assert "已准排" not in statuses
    assert statuses == ["影子意图"]
    assert sample_status == "已裁定"


def test_reconcile_and_proof_do_not_count_shadow_as_released(client):
    c, h, = client
    sample_id = _sample_id()

    proof = c.get(f"/api/proof/sample/{SAMPLE_CODE}", headers=h).json()
    # 影子意图未升正，证明页不得亮“已准排”灯
    assert proof["released"] is False
    assert proof["release_blocked"] is True
    assert all(r["status"] != "已准排" for r in proof["releases"])

    recon = c.get(f"/api/reconcile/{NEAR_PERMIT}", headers=h).json()
    # 对账页黄灯与许可页同源，且不得把影子行当正式准排
    assert recon["near_expiry_warn"] is True
    assert recon["release_blocked"] is True
    target = next(s for s in recon["samples"] if s["sample_code"] == SAMPLE_CODE)
    assert target["formally_released"] is False

    # 准排列表页同样只认真式行
    listed = c.get("/api/releases", headers=h).json()["items"]
    mine = [r for r in listed if r["sample_code"] == SAMPLE_CODE]
    assert all(r["status"] != "已准排" for r in mine)


def test_lock_helpers_leave_no_trace_on_contention_only(tmp_path, monkeypatch):
    """锁具自检：争用败方只记争用痕，不得产生任何准排行。"""
    monkeypatch.setattr("db.DB_PATH", tmp_path / "yi_lock.db")
    init_db()
    with connect() as conn:
        outfall_id = conn.execute(
            "SELECT id FROM outfalls WHERE outfall_code='OF-W2-A'"
        ).fetchone()["id"]
        cur = conn.execute(
            """INSERT INTO sample_batches(sample_code,outfall_id,permit_ref,metrics,flow_m3h,
               generation,status,adjudicated_at,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                "SM-BF05-YI-LOCK", outfall_id, NEAR_PERMIT,
                json.dumps({"cod": 30.0, "nh3n": 2.0, "tp": 0.2}),
                1200, 1, "已裁定", now_iso(), now_iso(),
            ),
        )
        sample_id = cur.lastrowid
        conn.commit()
    with connect() as conn:
        require_lock(conn, f"sample:{sample_id}", "持锁人甲")
        conn.commit()
    with connect() as conn:
        with pytest.raises(Exception):
            require_lock(conn, f"sample:{sample_id}", "争用败方乙")
        conn.commit()
    with connect() as conn:
        traces = conn.execute(
            "SELECT * FROM contention_log WHERE actor='争用败方乙'"
        ).fetchall()
        rows = conn.execute(
            "SELECT COUNT(*) c FROM discharge_releases WHERE sample_id=?",
            (sample_id,),
        ).fetchone()["c"]
    assert len(traces) == 1
    assert rows == 0  # 争用不催生任何准排行
    with connect() as conn:
        release(conn, f"sample:{sample_id}", "持锁人甲")
        conn.commit()
