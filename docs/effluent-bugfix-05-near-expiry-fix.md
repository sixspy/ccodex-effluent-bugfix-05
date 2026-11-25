# effluent-bugfix-05 临期修复说明（当日封存）

- 封存日期：2026-11-25
- 缺陷名：许可临期警告窗内准排写口仍出正式准排（许可页黄灯、对账误判已准排）
- 缺陷锚：准排写口只查许可存在、不查临期窗；许可详情黄灯为硬编码，`block_release` 恒为假
- 选定口径：**甲路径**——许可已入临期窗（或已过期）则准排整笔回退、拒收正式行；
  临期窗内只允许影子意图存在，正式区零改，待续期后另行原子升正。
  本修复不实现自动升正，避免任何“影子被对账当已准排”的旁门。
- 禁止项确认：未只改许可页黄灯；未引入绕过临期窗的写口；临期偏航一律记审计拒写痕。

## 唯一裁定口径（五页同源）

`backend/permit_window.py` 是临期窗唯一解释点，许可页、准排写口、对账/证明页、
采样页、排放口页全部调用 `permit_window_state()` / `is_release_blocked()`，
禁止各页自行解释 `valid_until`。

- 字段：`discharge_permits.valid_until`（YYYY-MM-DD）、`near_expiry_window_days`（默认 30）
- 临期：`today <= valid_until <= today + 窗长`；过期：`valid_until < today`
- `block_release = near_expiry_warn or expired`

## 改动清单

| 模块 | 改动 |
| --- | --- |
| `db.py` | 许可表加 `valid_until`、`near_expiry_window_days`；旧库 `ALTER TABLE` 迁移并回填在册许可 |
| `permit_window.py`（新增） | 临期窗唯一裁定函数 |
| `permits_svc.py` | 删除硬编码黄灯，列表/详情走同源裁定；登记许可支持有效期与窗长 |
| `release_svc.py` | 采样锁 + 许可锁双锁；临期窗联查，拒收正式行并写 `release_near_expiry_reject` 审计痕（先落痕后抛 400） |
| `locks.py` | 持锁人按会话区分（`session_holder`），同账号双会话互斥；并发插入竞态捕获 `IntegrityError` 转 409；争用痕先 commit 再抛，败方只留痕 |
| `reconcile_svc.py` | 对账带同源临期标志；采样行新增 `formally_released`，只认真式“已准排”，影子意图不计；证明页带 `release_blocked` |
| `samples_svc.py` | 采样列表/详情带同源临期标志（入库不拦，临期只拦准排） |
| `outfalls_svc.py` | 排放口带出同厂临期/阻断准排许可清单 |
| 前端五页 | 许可页黄灯+有效期列、准排页按钮与黄灯互斥、对账页临期列、采样页许可窗列、排放口页临期许可列，全部消费后端同源字段 |

## 验收口径（对应独验收段）

1. 临期窗内准排写口拒收：HTTP 400，正式 `discharge_releases` 零行，采样停留“已裁定”。
2. 对账假准排灯灭：影子意图行 `formally_released=false`，证明页 `released=false` 且 `release_blocked=true`。
3. 许可黄灯与准排按钮互斥：前端置灰 + 后端写口 400 双保险。
4. 双会话同口抢准排：一得一 409，败方仅在 `contention_log` 留争用痕，不落任何准排行。

## 测试闸

- 三红分文件，先见红后修复：
  - `tests/test_bf05_near_jia.py`（甲：临期仍出正式准排）
  - `tests/test_bf05_near_yi.py`（乙：影子意图误标已准排）
  - `tests/test_bf05_near_zh.py`（争用：同口准排锁）
- 并发脚本：`tests/duel_near_permit.py`（inprocess 真并发 + `--http` 真实 HTTP 两模式）
- 全量：`pytest -q` 16 passed；duel 两模式退出码均为 0。

## 偏离与偏航记录

- 无方案偏离：在题给甲/乙两条正确路径中选定甲路径并全程执行。
- 临期偏航拒写一律落审计 `action=release_near_expiry_reject`，不落正式行；无任何绕窗写口。
