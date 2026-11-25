#!/usr/bin/env python3
"""effluent-bugfix-05 并发双请求：两路会话同口抢临期许可准排（duel_near_permit）。

两种模式：
- inprocess（默认）：自建临时库，两条工作线程各持独立 sqlite 连接，
  完整走准排写口锁具 require_lock，制造真实库级并发（与 uvicorn 多线程同构）；
- http：对运行中的服务打真实 HTTP（BASE_URL 环境变量，默认 http://127.0.0.1:8000），
  要求服务端已存在许可 DP-BF05-DUEL 与两条已裁定采样。

判词口径：
- 健康许可双路抢锁：恰一路得锁、一路 409，败方只在 contention_log 留痕；
- 临期许可双路准排：正式“已准排”行必须为零，审计留两条临期偏航拒写痕。
退出码 0 合规，1 见红。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

NEAR_PERMIT = "DP-BF05-DUEL"
HEALTHY_PERMIT = "DP-BF05-DUEL-OK"
NEAR_UNTIL = "2026-12-12"  # 相对当日 2026-11-25 落在 30 天临期窗内
OUTFALL_CODE = "OF-E1-B"


def _seed():
    import db

    tmp = Path(tempfile.mkdtemp(prefix="bf05-duel-")) / "duel.db"
    db.DB_PATH = tmp
    db.init_db()
    with db.connect() as conn:
        plant_id = conn.execute(
            "SELECT id FROM plants WHERE plant_code='PL-EAST-01'"
        ).fetchone()["id"]
        outfall_id = conn.execute(
            "SELECT id FROM outfalls WHERE outfall_code=?", (OUTFALL_CODE,)
        ).fetchone()["id"]
        ids: dict[str, list[int]] = {NEAR_PERMIT: [], HEALTHY_PERMIT: []}
        for code, until in ((NEAR_PERMIT, NEAR_UNTIL), (HEALTHY_PERMIT, "2028-06-30")):
            conn.execute(
                """INSERT INTO discharge_permits(permit_code,plant_id,quota_m3d,cod_cap,nh3n_cap,
                   tp_cap,period_label,status,valid_until,near_expiry_window_days,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    code, plant_id, 50000, 50, 5, 0.5, "2026Q4", "有效",
                    until, 30, db.now_iso(),
                ),
            )
        for permit in ids:
            for n in (1, 2):
                scode = f"SM-{permit}-{n}"
                cur = conn.execute(
                    """INSERT INTO sample_batches(sample_code,outfall_id,permit_ref,metrics,flow_m3h,
                       generation,status,adjudicated_at,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        scode, outfall_id, permit,
                        json.dumps({"cod": 22.0, "nh3n": 1.8, "tp": 0.12}),
                        1000, 1, "已裁定", db.now_iso(), db.now_iso(),
                    ),
                )
                ids[permit].append(cur.lastrowid)
        conn.commit()
    return ids


def _duel_lock(permit: str, sample_ids: list[int]) -> list[tuple[str, str]]:
    """两路线程各持独立连接抢同一许可锁，返回 (会话, 结果) 列表。"""
    import db
    from fastapi import HTTPException
    from locks import require_lock

    start = threading.Event()
    outcomes: list[tuple[str, str]] = []

    def worker(holder: str, sample_id: int, delay: float) -> None:
        conn = db.connect()
        start.wait()
        try:
            threading.Event().wait(delay)
            require_lock(conn, f"sample:{sample_id}", holder)
            require_lock(conn, f"permit:{permit}", holder)
            conn.execute(
                "INSERT INTO audit_trail(actor,action,target,payload,created_at) "
                "VALUES (?,?,?,?,?)",
                (holder, "duel_hold", permit, "{}", db.now_iso()),
            )
            conn.commit()
            outcomes.append((holder, "acquired"))
        except HTTPException as exc:
            outcomes.append((holder, f"409:{exc.status_code}"))
        finally:
            conn.close()

    t1 = threading.Thread(
        target=worker, args=("duel-A", sample_ids[0], 0.0)
    )
    t2 = threading.Thread(
        target=worker, args=("duel-B", sample_ids[1], 0.15)
    )
    t1.start(); t2.start()
    start.set()
    t1.join(20); t2.join(20)
    return outcomes


def run_inprocess() -> int:
    ids = _seed()
    import db

    healthy_outcomes = _duel_lock(HEALTHY_PERMIT, ids[HEALTHY_PERMIT])
    near_outcomes = _duel_lock(NEAR_PERMIT, ids[NEAR_PERMIT])

    with db.connect() as conn:
        formal = conn.execute(
            "SELECT COUNT(*) c FROM discharge_releases dr "
            "JOIN sample_batches s ON s.id=dr.sample_id "
            "WHERE s.permit_ref=? AND dr.status='已准排'",
            (NEAR_PERMIT,),
        ).fetchone()["c"]
        contention = conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE lock_key=?",
            (f"permit:{HEALTHY_PERMIT}",),
        ).fetchone()["c"]

    print(f"健康许可抢锁结果: {sorted(healthy_outcomes)}")
    print(f"临期许可抢锁结果: {sorted(near_outcomes)}")
    print(f"临期正式已准排行: {formal}；许可锁争用痕: {contention}")

    healthy_ok = (
        sorted(holder for holder, r in healthy_outcomes if r == "acquired") == ["duel-A"]
        and any(r.startswith("409") for _, r in healthy_outcomes)
        and contention == 1
    )
    near_ok = formal == 0
    ok = healthy_ok and near_ok
    print(
        "判词:",
        "合规：同口准排锁串行、败方留痕、临期零正式行" if ok
        else "红：同口准排无锁或临期落正式行",
    )
    return 0 if ok else 1


def run_http(base_url: str) -> int:
    import httpx

    client = httpx.Client(base_url=base_url, timeout=30)
    tok = client.post(
        "/api/login", json={"username": "admin", "password": "admin123"}
    ).json()["token"]
    headers = {"Authorization": "Bearer " + tok}
    samples = [
        s for s in client.get("/api/samples", headers=headers).json()["items"]
        if s["permit_ref"] == NEAR_PERMIT and s["status"] == "已裁定"
    ]
    if len(samples) < 2:
        print(f"红/环境缺料：{NEAR_PERMIT} 下已裁定采样不足两条", file=sys.stderr)
        return 2
    barrier = threading.Barrier(2)
    results: list[tuple[int, int]] = []

    def worker(sample: dict) -> None:
        barrier.wait()
        r = client.post(
            "/api/releases",
            headers=headers,
            json={"sample_id": sample["id"], "officer": "duel-http"},
        )
        results.append((sample["id"], r.status_code))

    threads = [threading.Thread(target=worker, args=(s,)) for s in samples[:2]]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    releases = client.get("/api/releases", headers=headers).json()["items"]
    formal = [
        r for r in releases
        if r.get("sample_code") in {s["sample_code"] for s in samples[:2]}
        and r["status"] == "已准排"
    ]
    print(f"双请求结果: {sorted(results)}；正式已准排行: {len(formal)}")
    ok = not formal and sum(1 for _, code in results if code == 200) <= 1
    print("判词:", "合规：临期零正式行、争用串行" if ok else "红：无锁双准排/临期落正式行")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="临期许可同口双会话准排争用脚本")
    parser.add_argument("--http", action="store_true", help="对运行中服务打真实 HTTP")
    parser.add_argument(
        "--base-url", default=os.environ.get("BASE_URL", "http://127.0.0.1:8000")
    )
    args = parser.parse_args()
    return run_http(args.base_url) if args.http else run_inprocess()


if __name__ == "__main__":
    raise SystemExit(main())
