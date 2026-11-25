"""effluent-bugfix-05 甲红：许可已入临期警告窗，准排写口必须整笔回退、拒收正式行。

口径：甲路径——临期窗内不存在正式准排，许可黄灯与准排写口联查同一临期窗。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db

NEAR_PERMIT = "DP-BF05-NEAR-JIA"
NEAR_UNTIL = "2026-12-10"  # 相对当日 2026-11-25 落在 30 天临期窗内
HEALTHY_PERMIT = "DP-2026-E01"
SAMPLE_CODE = "SM-BF05-JIA-001"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "jia.db")
    init_db()
    c = TestClient(app)
    h = _auth(c)
    _make_near_permit()
    outfall_id = c.get("/api/outfalls", headers=h).json()["items"][0]["id"]
    created = c.post(
        "/api/samples",
        headers=h,
        json={
            "outfall_id": outfall_id,
            "permit_ref": NEAR_PERMIT,
            "sample_code": SAMPLE_CODE,
            "flow_m3h": 1000,
            "metrics": {"cod": 20.0, "nh3n": 1.5, "tp": 0.1},
        },
    ).json()
    assert created["status"] == "已裁定"
    return c, h, created["id"]


def _auth(c: TestClient, user="admin", password="admin123") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _make_near_permit() -> None:
    with connect() as conn:
        plant_id = conn.execute(
            "SELECT id FROM plants WHERE plant_code='PL-EAST-01'"
        ).fetchone()["id"]
        conn.execute(
            """INSERT INTO discharge_permits(permit_code,plant_id,quota_m3d,cod_cap,nh3n_cap,
               tp_cap,period_label,status,valid_until,near_expiry_window_days,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                NEAR_PERMIT, plant_id, 50000, 50, 5, 0.5, "2026Q4", "有效",
                NEAR_UNTIL, 30, "2026-11-01T10:00:00",
            ),
        )
        conn.commit()


def _release_rows(sample_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM discharge_releases WHERE sample_id=?", (sample_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def test_near_expiry_permit_must_reject_formal_release(client):
    c, h, sample_id = client

    # 许可侧：详情黄灯必须真来自临期窗，且 block_release 与黄灯同源
    permit = c.get(f"/api/permits/{NEAR_PERMIT}", headers=h).json()["permit"]
    assert permit["near_expiry_warn"] is True
    assert permit["block_release"] is True

    # 准排写口：临期窗内整笔回退，拒收正式行
    r = c.post(
        "/api/releases",
        headers=h,
        json={"sample_id": sample_id, "officer": "甲路验收"},
    )
    assert r.status_code == 400, f"临期窗内仍出正式准排：{r.status_code} {r.text}"
    assert "临期" in r.json()["detail"]

    # 正式区零改：无正式准排行、采样状态停留已裁定
    assert _release_rows(sample_id) == []
    with connect() as conn:
        status = conn.execute(
            "SELECT status FROM sample_batches WHERE id=?", (sample_id,)
        ).fetchone()["status"]
    assert status == "已裁定"

    # 拒写须留临期偏航痕（审计），但不得落成准排行
    audit = c.get("/api/audit?limit=50", headers=h).json()["items"]
    yaw = [a for a in audit if a["action"] == "release_near_expiry_reject"]
    assert len(yaw) == 1
    assert yaw[0]["target"] == SAMPLE_CODE


def test_healthy_permit_outside_window_still_releasable(client):
    c, h, _ = client
    healthy = next(
        s for s in c.get("/api/samples", headers=h).json()["items"]
        if s["permit_ref"] == HEALTHY_PERMIT and s["status"] == "已裁定"
    )
    permit = c.get(f"/api/permits/{HEALTHY_PERMIT}", headers=h).json()["permit"]
    assert permit["near_expiry_warn"] is False
    assert permit["block_release"] is False

    r = c.post(
        "/api/releases",
        headers=h,
        json={"sample_id": healthy["id"], "officer": "甲路对照"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "已准排"
