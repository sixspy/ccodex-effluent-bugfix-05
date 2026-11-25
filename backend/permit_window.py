"""许可临期警告窗的唯一裁定口径（五页同源）。

许可页黄灯、准排写口拒收、对账/证明假准排灯灭、采样页与排放口页的临期标记，
全部只能经由本模块判定，禁止各页自行解释有效期。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

DEFAULT_NEAR_EXPIRY_WINDOW_DAYS = 30


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def permit_window_state(
    permit, reference: date | None = None
) -> dict:
    """返回临期窗标志。permit 可为 sqlite3.Row 或映射，须含有效期与窗长字段。"""
    today = reference or date.today()
    valid_until = _parse_date(permit["valid_until"] if permit is not None else None)
    try:
        window = int(permit["near_expiry_window_days"]) if permit is not None else DEFAULT_NEAR_EXPIRY_WINDOW_DAYS
    except (KeyError, TypeError, ValueError):
        window = DEFAULT_NEAR_EXPIRY_WINDOW_DAYS
    if window <= 0:
        window = DEFAULT_NEAR_EXPIRY_WINDOW_DAYS

    active = permit is not None and permit["status"] == "有效"
    if valid_until is None:
        # 未登记有效期的在册许可不臆造临期，但也不得据此放行：按窗口字段缺省处理
        near = False
        expired = False
    else:
        near = active and today <= valid_until <= today + timedelta(days=window)
        expired = active and valid_until < today
    block_release = bool(near or expired)
    return {
        "valid_until": valid_until.isoformat() if valid_until else None,
        "near_expiry_window_days": window,
        "near_expiry_warn": bool(near),
        "expired": bool(expired),
        "block_release": block_release,
    }


def is_release_blocked(permit, reference: date | None = None) -> bool:
    return permit_window_state(permit, reference)["block_release"]
