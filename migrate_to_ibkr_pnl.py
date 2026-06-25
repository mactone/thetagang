#!/usr/bin/env python3
"""
DB 遷移：加入 realized_pnl 欄位，並將 IBKR 成本基礎損益寫入 executions。

執行後 /pnl 將改採 IBKR 相同的算法：
  - SLD 開倉：realized_pnl = 0（未平倉，不認列）
  - BOT 平倉：realized_pnl = IBKR cost-basis P&L
  - EXP 到期：realized_pnl = IBKR CRS 到期損益（新增記錄）

目標 YTD = $6,167（與 IBKR OPT realized_pnl 吻合）
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "thetagang.db"
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

# ─── BOT 平倉 exec_id → IBKR realized_pnl ────────────────────────────────────
# 正值 = 有獲利平倉；負值 = 虧損平倉
BOT_REALIZED_PNL = {
    "import-0001938b.6a20fe82.03.01":   -541.685970,  # TSLA BOT 13.40 (6/4) roll
    "import-0001938b.6a20fe83.03.01":    403.578580,  # TSLA BOT  6.45 (6/4) roll
    "import-0001938b.6a290caa.02.01":  1_006.709722,  # TSLA BOT  4.72 (6/10)
    "import-0001938b.6a2a3bbb.02.01":    973.709722,  # TSLA BOT  5.05 (6/11)
    "import-0000e242.6a337727.02.01.01": 317.388550,  # NVDA BOT  2.45 (6/18)
    "import-000182d0.6a3370e3.02.01.01": 154.393850,  # PLTR BOT  1.52 (6/18)
    "import-000182d0.6a3370e4.02.01.01": 108.156350,  # PLTR BOT  0.78 (6/18)
    "import-000182d0.6a33715a.02.01.01": 152.324250,  # PLTR BOT  1.34 (6/18 14:39:14)
    "import-000182d0.6a33715d.02.01.01":   0.0,       # PLTR BOT  1.34 (6/18 14:39:27)
    "import-0000e242.69b8d246.01.01":     0.0,        # NVDA BOT  3.05 (3/17) - IBKR 報 0
}

# ─── CRS 到期事件（新增記錄，side='EXP'）────────────────────────────────────────
# 格式：(exec_id, trade_time_utc, symbol, shares, realized_pnl)
CRS_EXPIRY_EVENTS = [
    ("import-CRS:1674309464", "2026-03-11T21:00:00Z", "TSLA", 1,   548.948760),
    ("import-CRS:1676560515", "2026-03-13T21:00:00Z", "NVDA", 2,   487.097520),
    ("import-CRS:1688858541", "2026-03-27T21:00:00Z", "PLTR", 1,   109.148760),
    ("import-CRS:1700310451", "2026-04-10T21:00:00Z", "NVDA", 1,   419.228760),
    ("import-CRS:1711974892", "2026-04-24T21:00:00Z", "PLTR", 1,   238.958760),
    ("import-CRS:1730942020", "2026-05-15T21:00:00Z", "PLTR", 1,   538.937636),
    ("import-CRS:1742801615", "2026-05-29T21:00:00Z", "TSLA", 1, 1_250.122989),
]


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # ── 1. 加入 realized_pnl 欄位（如已存在就跳過）──────────────────────────
    cur.execute("PRAGMA table_info(executions)")
    cols = {row[1] for row in cur.fetchall()}
    if "realized_pnl" not in cols:
        cur.execute("ALTER TABLE executions ADD COLUMN realized_pnl FLOAT")
        print("✓ 新增 realized_pnl 欄位")
    else:
        print("✓ realized_pnl 欄位已存在")

    # ── 2. 所有 SLD 記錄設 realized_pnl=0（開倉，未平倉不認列）──────────────
    cur.execute("UPDATE executions SET realized_pnl = 0 WHERE side = 'SLD' AND realized_pnl IS NULL")
    sld_updated = cur.rowcount
    print(f"✓ SLD 記錄設為 realized_pnl=0：{sld_updated} 筆")

    # ── 3. BOT 平倉記錄設入 IBKR 成本基礎損益 ─────────────────────────────
    bot_updated = 0
    for exec_id, pnl in BOT_REALIZED_PNL.items():
        cur.execute(
            "UPDATE executions SET realized_pnl = ? WHERE exec_id = ?",
            (pnl, exec_id),
        )
        if cur.rowcount:
            bot_updated += 1
        else:
            print(f"  ⚠️  未找到 exec_id={exec_id}（跳過）")
    print(f"✓ BOT 記錄更新 realized_pnl：{bot_updated} 筆")

    # ── 4. 取得 import run_id ──────────────────────────────────────────────
    cur.execute("SELECT id FROM runs WHERE config_path = 'ibkr-import' LIMIT 1")
    row = cur.fetchone()
    if not row:
        cur.execute("""
            INSERT INTO runs (started_at, config_path, dry_run, version, hostname)
            VALUES (?, 'ibkr-import', 0, 'import-v1', 'ibkr-mcp')
        """, (NOW,))
        run_id = cur.lastrowid
    else:
        run_id = row[0]

    # ── 5. 插入 CRS 到期事件（side='EXP'，含 realized_pnl）──────────────────
    crs_inserted = 0
    crs_skipped = 0
    for exec_id, trade_time, symbol, shares, pnl in CRS_EXPIRY_EVENTS:
        dt = datetime.fromisoformat(trade_time.replace("Z", "+00:00"))
        exec_time = dt.strftime("%Y-%m-%d %H:%M:%S")
        try:
            cur.execute("""
                INSERT INTO executions
                  (run_id, created_at, exec_id, symbol, side, shares, price,
                   execution_time, exchange, realized_pnl)
                VALUES (?, ?, ?, ?, 'EXP', ?, 0.0, ?, 'IBKR', ?)
            """, (run_id, NOW, exec_id, symbol, shares, exec_time, pnl))
            crs_inserted += 1
        except sqlite3.IntegrityError:
            crs_skipped += 1

    print(f"✓ CRS 到期事件插入：{crs_inserted} 筆，已存在跳過：{crs_skipped} 筆")

    conn.commit()
    conn.close()

    # ── 驗證：彙整 realized_pnl 總和 ────────────────────────────────────────
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT side,
               COUNT(*) as cnt,
               ROUND(SUM(realized_pnl), 2) as total_pnl
        FROM executions
        WHERE execution_time >= '2026-03-01'
        GROUP BY side
        ORDER BY side
    """)
    rows = cur.fetchall()
    conn.close()

    print(f"\n{'─'*55}")
    print(f"{'Side':<6}  {'筆數':>5}  {'realized_pnl 合計':>18}")
    print(f"{'─'*55}")
    grand = 0.0
    for side, cnt, total in rows:
        pnl_str = f"${total:>12,.2f}" if total else "$        0.00"
        print(f"{side:<6}  {cnt:>5}  {pnl_str}")
        grand += total or 0.0
    print(f"{'─'*55}")
    print(f"{'合計':<6}  {'':>5}  ${grand:>12,.2f}")

    target = 6_167.01
    diff = abs(grand - target)
    if diff < 1.0:
        print(f"\n✅ YTD realized_pnl = ${grand:,.2f}（目標 ${target:,.2f}，誤差 <$1）")
    else:
        print(f"\n⚠️  YTD realized_pnl = ${grand:,.2f}，與目標 ${target:,.2f} 差 ${diff:,.2f}")


if __name__ == "__main__":
    main()
