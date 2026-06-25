#!/usr/bin/env python3
"""
DB 遷移：加入 sec_type / right 欄位，讓 /attribution 能正確分 PUT vs CALL。

策略依據（已知 Wheel 組合）：
  TSLA → OPT CALL  (Covered Call CC)
  PLTR → OPT CALL  (Covered Call CC)
  NVDA → OPT PUT   (Cash-Secured Put CSP)
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "thetagang.db"


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # ── 1. 加入欄位（如已存在就跳過）──────────────────────────────────────────
    cur.execute("PRAGMA table_info(executions)")
    cols = {row[1] for row in cur.fetchall()}

    for col, typedef in [("sec_type", "VARCHAR"), ("right", "VARCHAR")]:
        if col not in cols:
            cur.execute(f"ALTER TABLE executions ADD COLUMN {col} {typedef}")
            print(f"✓ 新增 {col} 欄位")
        else:
            print(f"✓ {col} 欄位已存在")

    # ── 2. 補值：全部現有記錄都是 OPT（SGOV STK 未匯入）────────────────────
    cur.execute("UPDATE executions SET sec_type = 'OPT' WHERE sec_type IS NULL")
    print(f"✓ 設定 sec_type=OPT：{cur.rowcount} 筆")

    # ── 3. 依 symbol 設定 right（P=PUT / C=CALL）────────────────────────────
    mapping = {
        "NVDA": "P",   # CSP + protective put
        "TSLA": "C",   # Covered Call
        "PLTR": "C",   # Covered Call
    }
    for symbol, right in mapping.items():
        cur.execute(
            "UPDATE executions SET right = ? WHERE symbol = ? AND right IS NULL",
            (right, symbol),
        )
        print(f"✓ {symbol} right={right}：{cur.rowcount} 筆")

    conn.commit()

    # ── 驗證 ──────────────────────────────────────────────────────────────────
    cur.execute("""
        SELECT symbol, right, side, COUNT(*) as cnt,
               ROUND(SUM(CASE WHEN realized_pnl IS NOT NULL THEN realized_pnl ELSE 0 END), 2) AS pnl
        FROM executions
        GROUP BY symbol, right, side
        ORDER BY symbol, side
    """)
    print(f"\n{'Symbol':<6} {'Right':<6} {'Side':<5} {'Count':>5}  {'Realized PnL':>14}")
    print("─" * 50)
    for row in cur.fetchall():
        print(f"{row[0]:<6} {row[1] or '?':<6} {row[2]:<5} {row[3]:>5}  ${row[4]:>12,.2f}")

    conn.close()
    print("\n✅ 遷移完成，可重新測試 /attribution")


if __name__ == "__main__":
    main()
