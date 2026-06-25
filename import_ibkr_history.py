#!/usr/bin/env python3
"""
將 IBKR 歷史 Option 交易記錄匯入 thetagang DB。
exec_id 格式 'import-{trade_id}'，dashboard 會自動識別。
只匯入選擇權（OPT）的 SLD / BOT 交易，LEAPS 建倉和股票行使不納入。
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "thetagang.db"

# ─── IBKR Option 交易（SLD/BOT，排除 LEAPS 建倉、CRS、STK）───────────────────
# 格式：(trade_id, trade_time_utc, symbol, side, shares, price, exchange)
# side = "SLD" 賣出 / "BOT" 買回
IBKR_OPT_TRADES = [
    # ── 2026-03 ──────────────────────────────────────────────────────────────
    ("000182d0.69a66b19.01.01", "2026-03-03T14:30:52Z", "PLTR", "SLD", 1, 3.60,  "GEMINI"),
    ("0001938b.69a7c696.01.01", "2026-03-04T15:14:56Z", "TSLA", "SLD", 1, 5.50,  "PHLX"),
    ("0000e242.69a90dfa.01.01", "2026-03-05T14:30:03Z", "NVDA", "SLD", 2, 2.44,  "PSE"),
    ("0001938b.69b24467.01.01", "2026-03-12T14:30:15Z", "TSLA", "SLD", 1, 9.50,  "PHLX"),
    ("0000e242.69b39c1b.01.01", "2026-03-13T15:28:19Z", "NVDA", "SLD", 1, 4.20,  "SAPPHIRE"),
    ("000182d0.69b38f9d.01.01", "2026-03-13T15:32:38Z", "PLTR", "SLD", 1, 4.30,  "MEMX"),
    ("0000e242.69b8d246.01.01", "2026-03-17T13:30:38Z", "NVDA", "BOT", 1, 3.05,  "PSE"),
    ("000182d0.69ba289f.01.01", "2026-03-18T17:09:26Z", "PLTR", "SLD", 1, 1.10,  "NASDAQOM"),
    ("0001938b.69ba4ab9.01.01", "2026-03-18T16:35:23Z", "TSLA", "SLD", 1, 4.60,  "MIAX"),
    ("000182d0.69bb76e1.01.01", "2026-03-19T15:05:58Z", "PLTR", "SLD", 1, 2.40,  "EDGX"),
    # ── 2026-04 ──────────────────────────────────────────────────────────────
    ("000182d0.69d5d9eb.01.01", "2026-04-08T15:26:07Z", "PLTR", "SLD", 1, 2.04,  "ISE"),
    ("000182d0.69d5da57.01.01", "2026-04-08T16:16:12Z", "PLTR", "SLD", 1, 7.05,  "SAPPHIRE"),
    ("0001938b.69df35be.01.01", "2026-04-15T15:10:31Z", "TSLA", "SLD", 1, 12.25, "SAPPHIRE"),
    ("000182d0.69e1b46c.01.01", "2026-04-17T14:34:42Z", "PLTR", "SLD", 1, 5.40,  "PHLX"),
    ("0001938b.69eaeb17.01.01", "2026-04-24T13:30:09Z", "TSLA", "SLD", 1, 12.51, "NASDAQOM"),
    ("0001938b.69f2e8bf.01.01", "2026-04-30T15:01:53Z", "TSLA", "SLD", 1, 8.00,  "EDGX"),
    # ── 2026-05 ──────────────────────────────────────────────────────────────
    ("0001938b.69f44d45.01.01", "2026-05-01T15:31:46Z", "TSLA", "SLD", 1, 10.50, "SAPPHIRE"),
    # ── 2026-06 ──────────────────────────────────────────────────────────────
    ("0001938b.6a20fe82.02.01", "2026-06-04T14:00:23Z", "TSLA", "SLD", 1, 14.80, "CBOE2"),
    ("0001938b.6a20fe83.02.01", "2026-06-04T14:00:23Z", "TSLA", "SLD", 1, 14.80, "CBOE2"),
    ("0001938b.6a20fe82.03.01", "2026-06-04T14:00:23Z", "TSLA", "BOT", 1, 13.40, "CBOE2"),
    ("0001938b.6a20fe83.03.01", "2026-06-04T14:00:23Z", "TSLA", "BOT", 1, 6.45,  "CBOE2"),
    ("0000e242.6a226625.01.01", "2026-06-05T15:14:36Z", "NVDA", "SLD", 1, 5.64,  "BATS"),
    ("0001938b.6a290caa.03.01", "2026-06-10T15:44:39Z", "TSLA", "SLD", 1, 12.35, "CBOE2"),
    ("0001938b.6a290caa.02.01", "2026-06-10T15:44:39Z", "TSLA", "BOT", 1, 4.72,  "CBOE2"),
    ("0001938b.6a2a3bbb.03.01", "2026-06-11T14:02:12Z", "TSLA", "SLD", 1, 11.35, "CBOE2"),
    ("0001938b.6a2a3bbb.02.01", "2026-06-11T14:02:12Z", "TSLA", "BOT", 1, 5.05,  "CBOE2"),
    ("000182d0.6a2a3ab8.01.01", "2026-06-11T17:31:58Z", "PLTR", "SLD", 1, 1.88,  "EDGX"),
    ("000182d0.6a321e47.01.01", "2026-06-17T13:49:29Z", "PLTR", "SLD", 1, 2.88,  "EDGX"),
    ("000182d0.6a3223e7.01.01", "2026-06-17T15:34:07Z", "PLTR", "SLD", 1, 3.08,  "BATS"),
    ("0000e242.6a337727.02.01.01", "2026-06-18T14:22:54Z", "NVDA", "BOT", 1, 2.45, "EMERALD"),
    ("0000e242.6a337727.03.01.01", "2026-06-18T14:22:54Z", "NVDA", "SLD", 1, 3.00, "EMERALD"),
    ("000182d0.6a3370e3.02.01.01", "2026-06-18T14:22:36Z", "PLTR", "BOT", 1, 1.52, "EMERALD"),
    ("000182d0.6a3370e4.02.01.01", "2026-06-18T14:22:36Z", "PLTR", "BOT", 1, 0.78, "EMERALD"),
    ("000182d0.6a3370e3.03.01.01", "2026-06-18T14:22:36Z", "PLTR", "SLD", 1, 2.42, "EMERALD"),
    ("000182d0.6a3370e4.03.01.01", "2026-06-18T14:22:36Z", "PLTR", "SLD", 1, 2.42, "EMERALD"),
    ("000182d0.6a33715a.02.01.01", "2026-06-18T14:39:14Z", "PLTR", "BOT", 1, 1.34, "CBOE2"),
    ("000182d0.6a33715a.03.01.01", "2026-06-18T14:39:14Z", "PLTR", "SLD", 1, 2.35, "CBOE2"),
    ("000182d0.6a33715d.02.01.01", "2026-06-18T14:39:27Z", "PLTR", "BOT", 1, 1.34, "CBOE2"),
    ("000182d0.6a33715d.03.01.01", "2026-06-18T14:39:27Z", "PLTR", "SLD", 1, 2.38, "CBOE2"),
]

NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 建一個 import run（如不存在）
    cur.execute("SELECT id FROM runs WHERE config_path = 'ibkr-import' LIMIT 1")
    row = cur.fetchone()
    if row:
        run_id = row[0]
        print(f"✓ 使用現有 import run id={run_id}")
    else:
        cur.execute("""
            INSERT INTO runs (started_at, config_path, dry_run, version, hostname)
            VALUES (?, 'ibkr-import', 0, 'import-v1', 'ibkr-mcp')
        """, (NOW,))
        run_id = cur.lastrowid
        print(f"✓ 建立 import run id={run_id}")

    inserted = 0
    skipped = 0
    for trade_id, trade_time, symbol, side, shares, price, exchange in IBKR_OPT_TRADES:
        exec_id = f"import-{trade_id}"
        # 標準化時間格式
        dt = datetime.fromisoformat(trade_time.replace("Z", "+00:00"))
        exec_time = dt.strftime("%Y-%m-%d %H:%M:%S")

        try:
            cur.execute("""
                INSERT INTO executions
                  (run_id, created_at, exec_id, symbol, side, shares, price, execution_time, exchange)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (run_id, NOW, exec_id, symbol, side, shares, price, exec_time, exchange))
            inserted += 1
        except sqlite3.IntegrityError:
            skipped += 1  # 已存在（UNIQUE constraint on exec_id）

    conn.commit()
    conn.close()

    print(f"✓ 匯入完成：{inserted} 筆新增，{skipped} 筆已存在跳過")

    # 驗證
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT symbol, side, shares, price, round(shares*price*100,2), execution_time FROM executions ORDER BY execution_time")
    rows = cur.fetchall()
    conn.close()

    print(f"\n{'─'*72}")
    print(f"{'Symbol':<6} {'Side':<4} {'Qty':>3} {'Price':>7} {'Value':>9}  {'Time'}")
    print(f"{'─'*72}")
    total_sld = 0
    total_bot = 0
    for sym, side, shares, price, value, etime in rows:
        marker = "  ←" if side == "SLD" else ""
        print(f"{sym:<6} {side:<4} {int(shares):>3}  {price:>7.2f}  {value:>9,.2f}  {etime}{marker}")
        if side == "SLD":
            total_sld += value
        else:
            total_bot += value
    print(f"{'─'*72}")
    print(f"{'SLD 合計':>40}  ${total_sld:>10,.2f}")
    print(f"{'BOT 合計':>40}  ${total_bot:>10,.2f}")
    print(f"{'淨 Premium':>40}  ${total_sld - total_bot:>10,.2f}")

if __name__ == "__main__":
    main()
