#!/usr/bin/env python3
"""
IBKR 真實 P&L 分析 — 2026-03-01 起
數據來源：IBKR MCP get_account_trades (YEAR_TO_DATE, 2026-06-19 抓取)
"""

from datetime import datetime, timezone

# ─── 原始 IBKR 交易數據 ───────────────────────────────────────────────────────
TRADES = [
    # (trade_time, symbol, sec_type, side, size, price, commission, net_amount, realized_pnl, note)
    # 2026-03-02
    ("2026-03-02T14:30:03Z","PLTR","OPT","BUY",1,57.75,1.04795,5775,0,"PLTR LEAPS Jan27 100C — 建倉"),
    ("2026-03-02T14:30:00Z","TSLA","OPT","BUY",1,207.95,1.04795,20795,0,"TSLA LEAPS Jan27 200C — 建倉"),
    ("2026-03-03T14:30:14Z","SGOV","STK","BUY",100,100.40,1.00,10040,0,"SGOV 現金停泊"),
    # 2026-03-03
    ("2026-03-03T14:30:52Z","PLTR","OPT","SELL",1,3.60,0.70124,360,0,"PLTR SLD CSP/CC"),
    # 2026-03-04
    ("2026-03-04T15:14:56Z","TSLA","OPT","SELL",1,5.50,1.05124,550,0,"TSLA SLD CC"),
    # 2026-03-05
    ("2026-03-05T14:30:03Z","NVDA","OPT","SELL",2,2.44,0.90248,488,0,"NVDA SLD 2 contracts"),
    # 2026-03-11 CRS
    ("2026-03-11T20:20:00Z","TSLA","OPT","CRS",1,0,0,0,548.948760,"TSLA CC 到期 — 保留全部 premium"),
    # 2026-03-12
    ("2026-03-12T14:30:15Z","TSLA","OPT","SELL",1,9.50,1.05124,950,0,"TSLA SLD CC"),
    # 2026-03-13
    ("2026-03-13T15:28:19Z","NVDA","OPT","SELL",1,4.20,0.77124,420,0,"NVDA SLD"),
    ("2026-03-13T15:32:38Z","PLTR","OPT","SELL",1,4.30,0.56124,430,0,"PLTR SLD CC"),
    ("2026-03-13T20:20:00Z","NVDA","OPT","CRS",2,0,0,0,487.097520,"NVDA 到期"),
    ("2026-03-13T20:20:00Z","PLTR","OPT","CRS",1,0,0,0,0,"PLTR CC 到期/行使"),
    ("2026-03-13T20:20:00Z","PLTR","STK","CRS_SELL",100,150,0.0195,15000,0,"PLTR 100股@150 被叫走"),
    # 2026-03-17
    ("2026-03-17T13:30:38Z","NVDA","OPT","BUY",1,3.05,0.79795,305,0,"NVDA BOT 平倉"),
    # 2026-03-18
    ("2026-03-18T17:09:26Z","PLTR","OPT","SELL",1,1.10,0.85124,110,0,"PLTR SLD"),
    ("2026-03-18T16:35:23Z","TSLA","OPT","SELL",1,4.60,1.05124,460,0,"TSLA SLD CC"),
    # 2026-03-19
    ("2026-03-19T15:05:58Z","PLTR","OPT","SELL",1,2.40,1.04124,240,0,"PLTR SLD"),
    # 2026-03-25 CRS
    ("2026-03-25T20:20:00Z","TSLA","OPT","CRS",1,0,0,0,0,"TSLA CC 到期"),
    ("2026-03-25T20:20:00Z","TSLA","STK","CRS_BUY",100,387.5,0,38750,0,"TSLA PUT assigned @387.5"),
    # 2026-03-27 CRS
    ("2026-03-27T20:20:00Z","PLTR","OPT","CRS",1,0,0,0,109.148760,"PLTR PUT 到期 — 保留 premium"),
    ("2026-03-27T20:20:00Z","PLTR","OPT","CRS",1,0,0,0,0,"PLTR PUT assigned"),
    ("2026-03-27T20:20:00Z","PLTR","STK","CRS_BUY",100,145,0,14500,1288.718020,"PLTR PUT assigned @145 (含歷史累積 PnL)"),
    # 2026-04-08
    ("2026-04-08T15:26:07Z","PLTR","OPT","SELL",1,2.04,1.0554424,204,0,"PLTR SLD"),
    ("2026-04-08T16:16:12Z","PLTR","OPT","SELL",1,7.05,0.785763,705,0,"PLTR SLD"),
    # 2026-04-10 CRS
    ("2026-04-10T20:20:00Z","NVDA","OPT","CRS",1,0,0,0,419.228760,"NVDA 到期"),
    ("2026-04-10T20:20:00Z","PLTR","OPT","CRS",1,0,0,0,0,"PLTR CC 到期"),
    ("2026-04-10T20:20:00Z","PLTR","STK","CRS_BUY",100,145,0,14500,0,"PLTR PUT assigned @145 (2nd)"),
    ("2026-04-10T20:20:00Z","TSLA","OPT","CRS",1,0,0,0,0,"TSLA CC 到期"),
    ("2026-04-10T20:20:00Z","TSLA","STK","CRS_BUY",100,370,0,37000,0,"TSLA PUT assigned @370"),
    # 2026-04-15
    ("2026-04-15T15:10:31Z","TSLA","OPT","SELL",1,12.25,0.796475,1225,0,"TSLA SLD CC"),
    # 2026-04-17
    ("2026-04-17T14:34:42Z","PLTR","OPT","SELL",1,5.40,1.062364,540,0,"PLTR SLD CC"),
    # 2026-04-24
    ("2026-04-24T13:30:09Z","TSLA","OPT","SELL",1,12.51,0.8770106,1251,0,"TSLA SLD CC"),
    ("2026-04-24T20:20:00Z","PLTR","OPT","CRS",1,0,0,0,238.958760,"PLTR CC 到期"),
    # 2026-04-30
    ("2026-04-30T15:01:53Z","TSLA","OPT","SELL",1,8.00,1.05772,800,0,"TSLA SLD CC"),
    # 2026-05-01
    ("2026-05-01T15:31:46Z","TSLA","OPT","SELL",1,10.50,0.79317,1050,0,"TSLA SLD CC"),
    # 2026-05-08 CRS
    ("2026-05-08T20:20:00Z","PLTR","OPT","CRS",1,0,0,0,0,"PLTR PUT assigned"),
    ("2026-05-08T20:20:00Z","PLTR","STK","CRS_BUY",100,140,0,14000,0,"PLTR PUT assigned @140 (3rd)"),
    # 2026-05-15 CRS
    ("2026-05-15T20:20:00Z","TSLA","OPT","CRS",1,0,0,0,0,"TSLA CC 行使"),
    ("2026-05-15T20:20:00Z","TSLA","STK","CRS_SELL",100,405,0.8538,40500,3432.298485,"TSLA 100股@405 被叫走 — 股票獲利"),
    ("2026-05-15T20:20:00Z","PLTR","OPT","CRS",1,0,0,0,538.937636,"PLTR CC 到期"),
    # 2026-05-29 CRS
    ("2026-05-29T20:20:00Z","TSLA","OPT","CRS",1,0,0,0,1250.122989,"TSLA CC 到期"),
    # 2026-06-03 SGOV
    ("2026-06-03T14:17:51Z","SGOV","STK","BUY",10,100.42,1.00003,1004.2,0,"SGOV"),
    ("2026-06-03T13:57:58Z","SGOV","STK","BUY",10,100.42,1.00003,1004.2,0,"SGOV"),
    # 2026-06-04 TSLA roll
    ("2026-06-04T14:00:23Z","TSLA","OPT","BUY",1,13.40,0.62825,1340,-541.685970,"TSLA Roll — BOT 舊 CC (亦損)"),
    ("2026-06-04T14:00:23Z","TSLA","OPT","BUY",1,6.45,0.62825,645,403.578580,"TSLA Roll — BOT 舊 CC (小贏)"),
    ("2026-06-04T14:00:23Z","TSLA","OPT","SELL",1,14.80,0.662028,1480,0,"TSLA Roll — SLD 新 CC"),
    ("2026-06-04T14:00:23Z","TSLA","OPT","SELL",1,14.80,0.662028,1480,0,"TSLA Roll — SLD 新 CC"),
    # 2026-06-04 SGOV
    ("2026-06-04T14:22:27Z","SGOV","STK","BUY",10,100.43,1.00003,1004.3,0,"SGOV"),
    ("2026-06-04T16:02:52Z","SGOV","STK","BUY",10,100.43,1.00003,1004.3,0,"SGOV"),
    ("2026-06-04T16:03:45Z","SGOV","STK","BUY",10,100.43,1.00003,1004.3,0,"SGOV"),
    ("2026-06-04T17:37:10Z","SGOV","STK","BUY",10,100.43,1.00003,1004.3,0,"SGOV"),
    ("2026-06-04T19:03:25Z","SGOV","STK","BUY",10,100.43,0.00003,1004.3,0,"SGOV"),
    ("2026-06-04T19:25:57Z","SGOV","STK","BUY",4,100.43,0.000012,401.72,0,"SGOV"),
    # 2026-06-05
    ("2026-06-05T15:14:36Z","NVDA","OPT","SELL",1,5.64,0.8131584,564,0,"NVDA SLD 195P"),
    # 2026-06-08 SGOV sell
    ("2026-06-08T16:21:15Z","SGOV","STK","SELL",10,100.45,1.0226727,1004.5,-0.622673,"SGOV sell"),
    ("2026-06-08T17:24:45Z","SGOV","STK","SELL",10,100.46,0.02267476,1004.6,0.477325,"SGOV sell"),
    ("2026-06-08T18:02:52Z","SGOV","STK","SELL",10,100.46,0.02267476,1004.6,0.477325,"SGOV sell"),
    ("2026-06-08T18:39:00Z","SGOV","STK","SELL",10,100.46,0.02267476,1004.6,0.477325,"SGOV sell"),
    ("2026-06-08T19:11:40Z","SGOV","STK","SELL",10,100.46,0.02267476,1004.6,0.477325,"SGOV sell"),
    ("2026-06-08T19:25:35Z","SGOV","STK","SELL",10,100.46,0.02267476,1004.6,0.477325,"SGOV sell"),
    ("2026-06-08T19:37:03Z","SGOV","STK","SELL",10,100.46,0.02267476,1004.6,0.477325,"SGOV sell"),
    ("2026-06-08T19:50:10Z","SGOV","STK","SELL",20,100.46,0.04534952,2009.2,0.954650,"SGOV sell"),
    # 2026-06-09 SGOV buy
    ("2026-06-09T15:04:49Z","SGOV","STK","BUY",10,100.47,1.00003,1004.7,0,"SGOV"),
    ("2026-06-09T16:07:33Z","SGOV","STK","BUY",10,100.47,0.00003,1004.7,0,"SGOV"),
    ("2026-06-09T17:23:05Z","SGOV","STK","BUY",10,100.47,0.00003,1004.7,0,"SGOV"),
    ("2026-06-09T18:01:08Z","SGOV","STK","BUY",10,100.47,0.00003,1004.7,0,"SGOV"),
    ("2026-06-09T19:22:15Z","SGOV","STK","BUY",10,100.47,0.00003,1004.7,0,"SGOV"),
    # 2026-06-10 TSLA roll
    ("2026-06-10T15:44:39Z","TSLA","OPT","BUY",1,4.72,0.62825,472,1006.709722,"TSLA Roll — BOT 舊 CC"),
    ("2026-06-10T15:44:39Z","TSLA","OPT","SELL",1,12.35,0.656981,1235,0,"TSLA Roll — SLD 新 CC"),
    # 2026-06-11 TSLA roll + PLTR sell
    ("2026-06-11T14:02:12Z","TSLA","OPT","BUY",1,5.05,0.62825,505,973.709722,"TSLA Roll — BOT 舊 CC"),
    ("2026-06-11T14:02:12Z","TSLA","OPT","SELL",1,11.35,0.654921,1135,0,"TSLA Roll — SLD 新 CC"),
    ("2026-06-11T17:31:58Z","PLTR","OPT","SELL",1,1.88,1.0454128,188,0,"PLTR SLD CC"),
    # 2026-06-17 PLTR sell
    ("2026-06-17T13:49:29Z","PLTR","OPT","SELL",1,2.88,1.047473,288,0,"PLTR SLD CC"),
    ("2026-06-17T15:34:07Z","PLTR","OPT","SELL",1,3.08,0.807885,308,0,"PLTR SLD CC (DB 漏記)"),
    # 2026-06-18 NVDA + PLTR rolls + NVDA LEAPS
    ("2026-06-18T14:22:54Z","NVDA","OPT","BUY",1,2.45,0.79825,245,317.388550,"NVDA Roll — BOT 舊 PUT"),
    ("2026-06-18T14:22:54Z","NVDA","OPT","SELL",1,3.00,0.80772,300,0,"NVDA Roll — SLD 新 PUT"),
    ("2026-06-18T14:22:36Z","PLTR","OPT","BUY",1,1.52,0.79825,152,154.393850,"PLTR Roll — BOT"),
    ("2026-06-18T14:22:36Z","PLTR","OPT","BUY",1,0.78,0.79825,78,108.156350,"PLTR Roll — BOT"),
    ("2026-06-18T14:22:36Z","PLTR","OPT","SELL",1,2.42,0.806525,242,0,"PLTR Roll — SLD 新 CC"),
    ("2026-06-18T14:22:36Z","PLTR","OPT","SELL",1,2.42,0.806525,242,0,"PLTR Roll — SLD 新 CC"),
    ("2026-06-18T14:39:14Z","PLTR","OPT","BUY",1,1.34,0.62825,134,152.324250,"PLTR Roll — BOT"),
    ("2026-06-18T14:39:14Z","PLTR","OPT","SELL",1,2.35,0.636381,235,0,"PLTR Roll — SLD 新 CC"),
    ("2026-06-18T14:39:27Z","PLTR","OPT","BUY",1,1.34,0.62825,134,0,"PLTR Roll — BOT (open)"),
    ("2026-06-18T14:39:27Z","PLTR","OPT","SELL",1,2.38,0.636443,238,0,"PLTR Roll — SLD 新 CC"),
    ("2026-06-18T15:23:12Z","NVDA","OPT","BUY",1,61.33,0.79825,6133,0,"NVDA LEAPS Jun27 170C — 建倉"),
]

START_DATE = datetime(2026, 3, 1, tzinfo=timezone.utc)

# ─── 分析 ─────────────────────────────────────────────────────────────────────

def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

# 按類別分組
leaps_cost = []          # LEAPS 建倉成本（資本投入，非收益）
opt_sell = []            # 賣出選擇權 (premium received)
opt_buy_close = []       # 買回平倉 (roll 成本)
opt_crs = []             # 到期/行使 realized PnL
stk_crs = []             # 股票行使/分配 realized PnL
sgov = []                # SGOV 現金停泊
commissions_all = []

LEAPS_OI = {("PLTR","2026-03-02"), ("TSLA","2026-03-02"), ("NVDA","2026-06-18")}

for t in TRADES:
    trade_time, sym, sec, side, size, price, comm, net, rpnl, note = t
    dt = parse_time(trade_time)
    if dt < START_DATE:
        continue

    commissions_all.append((trade_time, sym, comm, note))

    if sec == "STK" and sym == "SGOV":
        sgov.append((trade_time, sym, side, size, price, net, rpnl, note))
        continue

    # 精確判斷 LEAPS：價格 > 40（roll 買回最高 $13.40，LEAPS 最低 $57.75）
    if sec == "OPT" and side == "BUY" and price > 40 and sym in ("PLTR","TSLA","NVDA"):
        # PLTR @ 57.75, TSLA @ 207.95, NVDA @ 61.33 都是 LEAPS
        leaps_cost.append((trade_time, sym, size, price, net, note))
        continue

    if sec == "OPT" and side == "SELL":
        opt_sell.append((trade_time, sym, size, price, net, comm, note))
    elif sec == "OPT" and side == "BUY":
        opt_buy_close.append((trade_time, sym, size, price, net, comm, rpnl, note))
    elif "CRS" in side:
        if sec == "OPT":
            if rpnl != 0:
                opt_crs.append((trade_time, sym, size, rpnl, note))
        elif sec == "STK":
            if rpnl != 0:
                stk_crs.append((trade_time, sym, side, size, price, rpnl, note))

# ─── 報表輸出 ─────────────────────────────────────────────────────────────────
SEP = "─" * 68

def header(title):
    print(f"\n{'═'*68}")
    print(f"  {title}")
    print('═'*68)

def fmt(v): return f"${v:>10,.2f}"
def fmtc(v): return f"${v:>8,.2f}"

print("\n" + "="*68)
print("  IBKR 真實收益分析 — 2026-03-01 ~ 2026-06-18")
print("  帳戶：YOUR_ACCOUNT_ID | 數據截止：2026-06-19 抓取")
print("="*68)

# ── 1. LEAPS 資本投入 ───────────────────────────────────────────────────────
header("1. LEAPS 建倉成本（資本投入，不計入收益）")
total_leaps = 0
for t in leaps_cost:
    trade_time, sym, size, price, net, note = t
    cost = net  # net_amount
    total_leaps += cost
    print(f"  {trade_time[:10]}  {sym:<5}  {size}×{price:>7.2f}  = {fmt(-cost)}   {note}")
print(SEP)
print(f"  {'LEAPS 總建倉成本':<40} {fmt(-total_leaps)}")

# ── 2. Option Premium 收入（SLD） ───────────────────────────────────────────
header("2A. Option Premium 毛收入（SLD = 賣出收取）")
total_sld = {"TSLA":0, "PLTR":0, "NVDA":0}
months_sld = {}
for t in opt_sell:
    trade_time, sym, size, price, net, comm, note = t
    month = trade_time[:7]
    months_sld.setdefault(month, {"TSLA":0,"PLTR":0,"NVDA":0})
    months_sld[month][sym] = months_sld[month].get(sym,0) + net
    total_sld[sym] = total_sld.get(sym,0) + net

print(f"  {'月份':<8} {'TSLA':>8} {'PLTR':>8} {'NVDA':>8} {'月合計':>10}")
print(f"  {'-'*46}")
grand_sld = 0
for m in sorted(months_sld):
    row = months_sld[m]
    t_val = row.get("TSLA",0)
    p_val = row.get("PLTR",0)
    n_val = row.get("NVDA",0)
    total = t_val + p_val + n_val
    grand_sld += total
    print(f"  {m}  {fmtc(t_val)}  {fmtc(p_val)}  {fmtc(n_val)}  {fmt(total)}")
print(SEP)
print(f"  {'合計':<8} {fmtc(total_sld['TSLA'])}  {fmtc(total_sld['PLTR'])}  {fmtc(total_sld['NVDA'])}  {fmt(grand_sld)}")

# ── 3. 買回平倉成本（BOT） ────────────────────────────────────────────────
header("2B. Option 買回平倉成本（BOT — roll 付出）")
total_bot = 0
for t in opt_buy_close:
    trade_time, sym, size, price, net, comm, rpnl, note = t
    total_bot += net
    rpnl_str = f"PnL={fmt(rpnl)}" if rpnl != 0 else ""
    print(f"  {trade_time[:10]}  {sym:<5}  -{fmtc(net)}   {note}")
print(SEP)
print(f"  {'買回成本合計':<40} {fmt(-total_bot)}")

net_option_premium = grand_sld - total_bot
print(f"  {'淨 Option Premium (SLD - BOT)':<40} {fmt(net_option_premium)}")

# ── 4. 到期/行使 Realized PnL ──────────────────────────────────────────────
header("3. 到期 / 行使 已實現 PnL（IBKR reported）")
total_crs_opt = 0
for t in opt_crs:
    trade_time, sym, size, rpnl, note = t
    total_crs_opt += rpnl
    print(f"  {trade_time[:10]}  {sym:<5}  {fmt(rpnl)}   {note}")
print(SEP)
print(f"  {'OPT 到期 累積 PnL':<40} {fmt(total_crs_opt)}")

# ── 5. 股票行使 ──────────────────────────────────────────────────────────────
header("4. 股票行使 / Assignment 已實現 PnL")
total_stk_crs = 0
for t in stk_crs:
    trade_time, sym, side, size, price, rpnl, note = t
    total_stk_crs += rpnl
    flag = "  ⚠️ (疑為含歷史 premium)" if rpnl == 1288.718020 else ""
    print(f"  {trade_time[:10]}  {sym:<5}  {size}股@{price}  {fmt(rpnl)}{flag}")
    print(f"            {note}")
print(SEP)
print(f"  {'股票行使 累積 PnL':<40} {fmt(total_stk_crs)}")

# ── 6. SGOV ───────────────────────────────────────────────────────────────────
total_sgov_pnl = sum(t[6] for t in sgov)
print(f"\n{'─'*68}")
print(f"  SGOV 現金停泊 realized PnL                   {fmt(total_sgov_pnl)}")

# ── 7. 手續費 ─────────────────────────────────────────────────────────────────
# 計算選擇權交易手續費（不含 LEAPS 建倉、SGOV）
total_comm = sum(c for _,_,c,_ in commissions_all)
print(f"  手續費合計（含 LEAPS 建倉）                   {fmt(-total_comm)}")

# ── 8. 彙總 ─────────────────────────────────────────────────────────────────
header("★  收益彙總")

# 純 option realized（同 Obsidian 方法）
pure_opt_realized = total_crs_opt + sum(
    t[6] for t in opt_buy_close if t[6] != 0
)
print(f"""
  ┌─────────────────────────────────────────────────────────┐
  │  計算方式 A：純 Option Realized（IBKR realized_pnl 欄）  │
  │  — 和 Obsidian 文件定義相同，不含股票行使獲利              │
  ├─────────────────────────────────────────────────────────┤
  │  OPT 到期 realized PnL         {fmt(total_crs_opt):>12}              │
  │  Roll 平倉 realized PnL        {fmt(sum(t[6] for t in opt_buy_close if t[6]!=0)):>12}              │
  │  SGOV P&L                      {fmt(total_sgov_pnl):>12}              │
  ├─────────────────────────────────────────────────────────┤
  │  純 Option 累積收益             {fmt(pure_opt_realized + total_sgov_pnl):>12}              │
  └─────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────┐
  │  計算方式 B：含股票獲利的總 Realized PnL                  │
  ├─────────────────────────────────────────────────────────┤
  │  純 Option Realized             {fmt(pure_opt_realized):>12}              │
  │  TSLA CC 行使股票獲利 (5/15)   {fmt(3432.298485):>12}              │
  │  PLTR Assignment PnL (3/27)    {fmt(1288.718020):>12}  ⚠️           │
  │  SGOV P&L                      {fmt(total_sgov_pnl):>12}              │
  ├─────────────────────────────────────────────────────────┤
  │  總 Realized PnL               {fmt(pure_opt_realized + 3432.298485 + 1288.718020 + total_sgov_pnl):>12}              │
  └─────────────────────────────────────────────────────────┘
""")

print(f"  ⚠️  注意：PLTR 3/27 +$1,288.72 是 IBKR 的 CRS 結算，")
print(f"       可能包含 3/1 前的歷史 premium，不建議全額計入。")
print(f"       扣除後 Total Realized = {fmt(pure_opt_realized + 3432.298485 + total_sgov_pnl)}")

print(f"\n  毛 Option Premium 收入（SLD）               {fmt(grand_sld)}")
print(f"  扣除 Roll 買回成本（BOT）                   {fmt(-total_bot)}")
print(f"  淨 Premium（尚含未到期部位）                {fmt(net_option_premium)}")
print(f"\n  手續費總支出                                {fmt(-total_comm)}")

print(f"\n{'─'*68}")
print(f"  [Obsidian 文件比對]")
print(f"  文件 Gross Premium 合計  $11,853  (真實: {fmt(grand_sld)}) ← 漏記6月TSLA roll")
print(f"  文件 Net Realized        $6,166   (真實: {fmt(pure_opt_realized + total_sgov_pnl)})")
print(f"{'─'*68}\n")
