# ThetaGang — Wheel / PMCC 自動化現金流機器人（Mactone Fork）

本分支在上游 ThetaGang 基礎上新增了完整的 **Telegram 控制介面**：即時帳戶監控、動態修改策略配置、倉位管理、以及詳細的權利金收入儀表板——全部透過手機操作，無需登入伺服器。

Docker 容器內整合 IB Gateway（透過 IBC）、每小時執行的交易引擎，以及 Telegram Bot 常駐程序。

---

## 環境需求

| 項目 | 說明 |
|---|---|
| Docker ≥ 24 | IB Gateway + IBC 已打包在映像內，無需另行安裝 |
| IBKR 帳戶 | 模擬或真實帳戶；需開啟 API 存取權限 |
| Telegram Bot | 透過 [@BotFather](https://t.me/BotFather) 建立；記下 `bot_token` 與你的 `chat_id` |

---

## 從零到運行：逐步說明

### 1. Clone 專案

```bash
git clone https://github.com/mactone/thetagang.git
cd thetagang
```

### 2. 打包 Python 套件（Docker build 需要）

Dockerfile 從本地 `.whl` 安裝套件，需先建置：

```bash
pip install uv
uv build
# → 產出 dist/thetagang-*.whl
```

### 3. 建立設定檔（已加入 .gitignore，不會被提交）

**`thetagang.toml`** — 主設定檔，最少必填欄位：

```toml
[account]
account = "YOUR_ACCOUNT_ID"        # 真實帳號如 U1234567；模擬如 DU1234567
cancel_orders = true
market_data_type = 1               # 1=即時, 3=延遲免費

[ibc]
userid      = "YOUR_TWS_USERNAME"
password    = "YOUR_TWS_PASSWORD"
tradingMode = "paper"              # "paper" 模擬 / "live" 真實

[telegram]
enabled   = true
bot_token = "YOUR_BOT_TOKEN"
chat_id   = "YOUR_CHAT_ID"
password  = "YOUR_BOT_PASSWORD"    # 防止他人使用你的 Bot

[thetagang]
minimum_credit = 0.10              # 低於 $0.10/股 信用的訂單跳過不送出

[roll_when]
pnl          = 0.50   # 到達最大利潤 50% 時 roll
dte          = 7      # 或距到期僅剩 7 天時 roll
close_at_pnl = 0.90  # 到達 90% 利潤直接平倉（不 roll）

[symbols.TSLA]
weight = 0.50
# 繼續新增標的，所有 weight 加總需等於 1.0
```

**`ibc-config.ini`** — IBC 自動登入設定，從 repo 內的範例檔複製，設定 `TradingMode=paper` 或 `TradingMode=live`。

### 4. 建置映像並啟動容器

```bash
./run_docker.sh
```

等效的手動指令：

```bash
docker build -t thetagang .

docker run -d --name thetagang-bot \
  --restart unless-stopped \
  -v "$PWD/thetagang.toml:/etc/thetagang/thetagang.toml" \
  -v "$PWD/ibc-config.ini:/etc/thetagang/ibc-config.ini" \
  -v "$PWD/data:/etc/thetagang/data" \
  thetagang \
  --config /etc/thetagang/thetagang.toml --bot
```

### 5. 確認啟動成功

```bash
docker logs -f thetagang-bot
```

正常啟動順序：
1. Xvfb 虛擬顯示啟動
2. IBC Daemon 啟動 IB Gateway（首次需等 30–120 秒）
3. 日誌出現 `"TWS is ready on port 7497"`
4. 交易引擎執行第一個週期
5. Telegram Bot 啟動 → 收到 `⚡ ThetaGang container started` 通知

傳送 `/status` 給你的 Bot，確認有回應即完成。

---

## 交易時段

引擎每小時觸發一次，但只在**開市視窗**內執行：

- **週一至週五 13:00–21:00 UTC**（NYSE 13:30–20:00 UTC，前後各留 30 分鐘緩衝）
- 視窗外自動休眠至下一個交易日

---

## 更新程式碼（無需完整重建）

容器將 Python 套件烤進映像的 `/opt/venv/`——在 host 修改原始碼後，**重啟容器無效**。

**單檔快速更新：**
```bash
docker cp thetagang/telegram_bot.py \
  thetagang-bot:/opt/venv/lib/python3.14/site-packages/thetagang/telegram_bot.py
docker stop thetagang-bot && docker start thetagang-bot
```

**完整重建（結構性變更）：**
```bash
uv build && ./run_docker.sh
```

---

## Telegram 指令參考

### 狀態與總覽

| 指令 | 說明 |
|---|---|
| `/0start` | 快速概覽——僅顯示最常用指令 |
| `/start` | 完整指令說明選單 |
| `/status` | 帳戶摘要：NAV、淨清算價值、現金、保證金使用率 |
| `/positions` | 所有持倉：Greek 值、成本基礎、未實現損益 |
| `/trades` | 最近 3 天的成交紀錄 |
| `/orders` | 目前在券商端的所有掛單（詳細） |

### 收入與損益追蹤

| 指令 | 說明 |
|---|---|
| `/revenue` | **月度權利金帳本**——已實現淨損益 + 未平倉待結算（見下方解讀說明） |
| `/pnl` | 已實現期權損益：今日 / 本週 / 本月 / 年初至今 |
| `/attribution` | 損益分類：賣 Put 收入 / 賣 Call 收入 / Roll 成本 / 股票損益 |

### 持倉分析

| 指令 | 說明 |
|---|---|
| `/expirations` | 未來 60 天到期的期權清單 |
| `/theta` | 各持倉每日 Theta 衰減金額 |
| `/greeks` | 投資組合整體 Greeks：Delta / Gamma / Theta / Vega |
| `/iv <symbol>` | IV Rank + 52 週 IV 歷史 |
| `/wheel_check` | 掃描缺口：缺 CC、缺 PMCC、ITM 警示、DTE/PnL Roll 觸發 |
| `/nav` | NAV 對帳：股票 + 期權 + 現金 vs 初始資金 |

### PMCC / LEAPS

| 指令 | 說明 |
|---|---|
| `/leaps <symbol>` | 建議最佳 LEAPS Call 履約價（用於 PMCC） |
| `/buy_leaps <symbol> <YYYYMMDD> <strike>` | 下 LEAPS Call 買入訂單（例：`/buy_leaps NVDA 20270115 170`） |

### 即時策略調整

| 指令 | 說明 |
|---|---|
| `/strategy` | 當前各標的權重與暫停狀態 |
| `/settings` | 保證金限制、Delta 目標、現金/SGOV 配置、避險設定 |
| `/set_weight <symbol> <percent>` | 草擬新的目標權重（例：`/set_weight TSLA 40`） |
| `/set_no_trading <symbol> <true\|false>` | 草擬停止某標的交易 |
| `/preview_config` | 查看待套用的設定差異 |
| `/apply_config` | 確認並寫入 `thetagang.toml` |
| `/discard_config` | 放棄草稿 |
| `/reload_strategy` | 將 TOML 重新載入 Telegram Bot Daemon |

### 暫停 / 恢復

| 指令 | 說明 |
|---|---|
| `/pause <symbol\|all>` | 暫停某標的（或全部）的自動下單 |
| `/resume <symbol\|all>` | 恢復某標的（或全部）的自動下單 |

### 訂單管理

| 指令 | 說明 |
|---|---|
| `/close <conId\|symbol>` | 對某持倉送出平倉訂單 |
| `/cancel_order <orderId>` | 取消指定掛單 |
| `/modify_order <orderId> <newPrice>` | 修改掛單的限價 |

### 歷史與診斷

| 指令 | 說明 |
|---|---|
| `/history [N]` | 最近 N 次交易引擎執行摘要（預設 5 次） |
| `/events [symbol]` | 近期引擎決策事件，可依標的篩選 |

---

## 如何解讀 Telegram 輸出

### `/revenue` — 月度權利金帳本

```
📊 Monthly Premium Ledger
2025-03   Realized: $XXX    Pending: $XXX
2025-04   Realized: $XXX    Pending: $XXX
...
Realized Avg/mo: $1,541.75
ℹ️ 已實現=IBKR稅務成本淨損益（含roll成本）；未平倉=原始收取premium待結算。
```

| 欄位 | 資料來源 | 可作為現金流規劃？ |
|---|---|---|
| **Realized（已實現）** | `executions.realized_pnl`——IBKR 稅務成本淨損益，Roll 成本已扣除 | **是**——確認入袋的現金 |
| **Pending（待結算）** | 尚未平倉的倉位原始收取金額 | 否——倉位仍在，結果未定 |
| **Realized Avg/mo** | 已實現欄位的月平均值 | **是**——現金流規劃的基準數字 |

> 重要：**不要**用 Pending 規劃實際現金支出。倉位可能 Roll（付出 Debit）或被行使，最終損益尚未確定。

### `/nav` — NAV 對帳

```
📊 NAV Reconciliation
股票市值：    $XX,XXX
期權市值：    $XX,XXX   (LEAPS + 賣出期權 mark-to-market)
現金：        $XX,XXX
───────────────────────
總 NAV：      $XX,XXX
初始資金：    $XX,XXX
變動：        +$X,XXX (+X.X%)
```

**為何 NAV 增幅 ≠ 收取的權利金？**

賣出期權收取現金的同時，期權負債（mark-to-market 虧損）會抵消現金增加。NAV 要等期權時間價值衰退才能真正上升。此外，股票與 LEAPS 的未實現虧損會直接拖累 NAV。

範例：收取 $14,896 毛權利金，但 LEAPS 與股票倉位未實現虧損 $8,649 → NAV 實際僅增加 $6,247。

### `/pnl` — 快速損益

```
Today:   $XX.XX
Week:    $XXX.XX
Month:   $XXX.XX
YTD:     $X,XXX.XX
```

與 `/revenue` 同一資料來源（`realized_pnl`），Roll 成本已內扣。可直接用於現金流參考。

### `/wheel_check` — 缺口掃描

| 標記 | 含義 |
|---|---|
| `Missing CC` | 持有股票但未賣 Covered Call |
| `Missing PMCC` | 持有 LEAPS 但未賣對應的 Short Call |
| `ITM` | 賣出的 Put 或 Call 已進入價內（有被行使風險） |
| `DTE trigger` | 距到期僅剩 `roll_when.dte` 天（建議考慮 Roll） |
| `PnL trigger` | 已達 `roll_when.pnl` 獲利目標（可平倉或 Roll） |

### `/attribution` — 損益分類

```
Put premium:   +$X,XXX   (CSP / Wheel Put 收入)
Call premium:  +$X,XXX   (CC / PMCC Call 收入)
Roll cost:     -$XXX     (Roll 時支付的 Debit)
Stock P&L:     -$X,XXX   (股票 / LEAPS 未實現變動)
Net total:     +$X,XXX
```

可用來判斷收入是否被 Roll 成本或股票拖累吃掉。

---

## 關鍵數字指引

| 數字 | 可動用現金？ | 說明 |
|---|---|---|
| `/revenue` Realized | **是** | IBKR 淨損益，Roll 成本已扣 |
| `/pnl` YTD | **是** | 同上 |
| `/revenue` Pending | 否 | 倉位仍開，結果未定 |
| 毛 SLD 現金流 | 否 | 含未配對的 Roll Debit |
| NAV 變動 | 否 | 含股票未實現波動 |

---

## Roll 邏輯

在 `[roll_when]` 設定：

```
pnl          = 0.50   → 達到最大利潤 50% 時 Roll
dte          = 7      → 或剩餘 ≤ 7 天時 Roll
close_at_pnl = 0.90  → 達 90% 利潤直接平倉（不 Roll）
minimum_credit = 0.10 → 信用 < $0.10/股的訂單跳過不送
```

引擎在市場時段每小時自動檢查並送出限價單。

---

## 回測腳本

以下為純分析腳本，不連接 IBKR，不下單：

| 腳本 | 用途 |
|---|---|
| `backtest_current_params_10y.py` | 以當前參數跑 10 年歷史 NAV 曲線 |
| `backtest_conservative_compare.py` | 比較當前、保守、防禦型三種參數的回測結果 |
| `estimate_call_put_premiums.py` | 估算各情境下的 Put + Call 權利金報酬率 |

```bash
pip install numpy pandas yfinance scipy matplotlib
python backtest_current_params_10y.py
# 輸出至 ./output/backtest/（或 $THETAGANG_OUT_DIR）
```

---

## 資料檔說明（已加入 .gitignore，不會提交）

| 檔案 | 內容 |
|---|---|
| `data/thetagang.db` | SQLite：成交紀錄、持倉快照、訂單、NAV 歷史 |
| `data/telegram_fill_monitor_state.json` | 最後讀取的成交 ID，用於推送新成交通知 |
| `thetagang.toml` | 機密：IBKR 帳號、IBC 登入、Telegram Token |
| `ibc-config.ini` | IBC 自動化登入設定 |

---

## 故障排除

**Bot 無回應：** `docker logs thetagang-bot | tail -50`

**修改程式碼後無效：** 容器使用的是映像內的版本。用 `docker cp` + stop/start 快速更新，或 `./run_docker.sh` 完整重建。

**TWS port 7497 未就緒：** IBC 首次啟動需 60–120 秒。查看日誌中是否出現 `"TWS is ready"`。

**訂單被跳過（below minimum_credit）：** 調整 `thetagang.toml` 中的 `minimum_credit`。

---

> 完整英文文件請見 [README.md](README.md)
