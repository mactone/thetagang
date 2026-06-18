import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import difflib
import html
import logging
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, Sequence

import tomlkit
from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from thetagang.config import Config

logger = logging.getLogger(__name__)

TAIPEI_TZ = timezone(timedelta(hours=8))
ORDER_FILL_MONITOR_INTERVAL_SECONDS = 5 * 60

# Global config variables
config: Optional[Config] = None
config_path: Optional[str] = None


def _money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _pct(value: float) -> str:
    return f"{value:+.1f}%"


def _compact_money(value: float) -> str:
    sign = "-" if value < 0 else ""
    abs_value = abs(value)
    if abs_value >= 1_000_000:
        return f"{sign}${abs_value / 1_000_000:.2f}M"
    if abs_value >= 1_000:
        return f"{sign}${abs_value / 1_000:.1f}k"
    return f"{sign}${abs_value:.2f}"


def _risk_label(cushion: float) -> tuple[str, str]:
    cushion_pct = cushion * 100
    if cushion >= 0.40:
        return "🟢 Healthy", f"Cushion {cushion_pct:.1f}% &gt; 40%"
    if cushion >= 0.30:
        return "🟡 Watch", f"Cushion {cushion_pct:.1f}% between 30–40%"
    if cushion >= 0.20:
        return "🟠 Reduce risk", f"Cushion {cushion_pct:.1f}% between 20–30%"
    return "🔴 Danger", f"Cushion {cushion_pct:.1f}% &lt; 20%"


def _format_status_message(
    net_liq: float,
    cash: float,
    buying_power: float,
    excess_liq: float,
    maint_margin: float,
    accrued: float,
    cushion: float,
) -> str:
    margin_usage = maint_margin / net_liq * 100 if net_liq else 0
    cash_pct = cash / net_liq * 100 if net_liq else 0
    buying_power_x = buying_power / net_liq if net_liq else 0
    excess_to_maint = excess_liq / maint_margin if maint_margin else 0
    risk_label, risk_reason = _risk_label(cushion)

    if cushion >= 0.40:
        action = "可正常運作；新倉仍建議用小部位，避免一次吃滿 Buying Power。"
    elif cushion >= 0.30:
        action = "暫緩加大新倉；優先觀察高波動標的與短 DTE 空方選擇權。"
    else:
        action = "建議降低曝險或補現金；避免新增 short premium 部位。"

    return (
        "📊 <b>Account Risk Dashboard</b>\n\n"
        f"{risk_label} — <b>{risk_reason}</b>\n"
        f"Action: {action}\n\n"
        "💰 <b>Balances</b>\n"
        f"• NAV: <b>{_money(net_liq)} USD</b>\n"
        f"• Cash: <b>{_money(cash)} USD</b> ({cash_pct:.2f}% NAV)\n"
        f"• Buying Power: <b>{_money(buying_power)} USD</b> ({buying_power_x:.2f}× NAV)\n"
        f"• Accrued Cash: <b>{_money(accrued)} USD</b>\n\n"
        "🛡 <b>Margin Safety</b>\n"
        f"• Excess Liquidity: <b>{_money(excess_liq)} USD</b>\n"
        f"• Maintenance Margin: <b>{_money(maint_margin)} USD</b> "
        f"({margin_usage:.1f}% NAV)\n"
        f"• Excess / Maint. Margin: <b>{excess_to_maint:.2f}×</b>\n"
        f"• Cushion: <b>{cushion * 100:.1f}%</b>"
    )


def _format_today_fill(fill: Any) -> str:
    contract = getattr(fill, "contract", None)
    execution = getattr(fill, "execution", None)
    symbol = html.escape(getattr(contract, "symbol", "-"))
    side = getattr(execution, "side", "-")
    shares = float(getattr(execution, "shares", 0) or 0)
    price = float(getattr(execution, "price", 0) or 0)
    when_dt = _execution_datetime(fill, execution)
    if when_dt.tzinfo is not None:
        when_dt = when_dt.astimezone(timezone(timedelta(hours=8)))
    when = when_dt.strftime("%m-%d %H:%M")
    return f"• <code>{when} TW</code> {side} <b>{shares:g} {symbol}</b> @ {_money(price)}"


def _format_open_trade(trade: Any) -> str:
    contract = getattr(trade, "contract", None)
    order = getattr(trade, "order", None)
    status = getattr(trade, "orderStatus", None)
    symbol = html.escape(getattr(contract, "symbol", "-"))
    action = getattr(order, "action", "-")
    qty = float(getattr(order, "totalQuantity", 0) or 0)
    price = getattr(order, "lmtPrice", None)
    status_text = getattr(status, "status", "-")
    filled = float(getattr(status, "filled", 0) or 0)
    remaining = float(getattr(status, "remaining", 0) or 0)
    price_text = "MKT" if price in (None, 0) else _money(float(price))
    return (
        f"• {action} <b>{qty:g} {symbol}</b> @ {price_text} — {status_text} "
        f"(filled {filled:g}, remaining {remaining:g})"
    )


def _format_submitted_order(order: dict[str, Any]) -> str:
    symbol = html.escape(str(order.get("symbol") or "-"))
    action = html.escape(str(order.get("action") or "-"))
    qty = float(order.get("quantity") or 0)
    price = order.get("limit_price")
    status_text = html.escape(str(order.get("status") or "Submitted"))
    filled = float(order.get("filled") or 0)
    remaining = float(order.get("remaining") or 0)
    order_id = order.get("order_id")
    created_at = order.get("created_at")
    price_text = "MKT" if price in (None, 0) else _money(float(price))
    age_text = f" | <code>{html.escape(str(created_at)[:16])}</code>" if created_at else ""
    return (
        f"• {action} <b>{qty:g} {symbol}</b> @ {price_text} — {status_text} "
        f"(filled {filled:g}, remaining {remaining:g}, id {order_id}){age_text}"
    )


def _load_submitted_unfilled_orders(
    *,
    exclude_order_ids: Sequence[int] = (),
    lookback_days: int = 7,
    limit: int = 8,
) -> list[dict[str, Any]]:
    if not config or not config_path or not config.runtime.database.enabled:
        return []

    from sqlalchemy import bindparam, create_engine, text

    db_url = config.runtime.database.resolve_url(config_path)
    engine = create_engine(db_url, future=True)
    cutoff = datetime.now() - timedelta(days=lookback_days)
    working_statuses = ("PendingSubmit", "PreSubmitted", "Submitted")

    query = text(
        """
        WITH latest_status AS (
            SELECT os.*
            FROM order_statuses os
            JOIN (
                SELECT order_id, MAX(created_at) AS max_created_at
                FROM order_statuses
                WHERE order_id IS NOT NULL
                GROUP BY order_id
            ) latest
              ON latest.order_id = os.order_id
             AND latest.max_created_at = os.created_at
        ), execution_totals AS (
            SELECT order_id, SUM(shares) AS executed_shares
            FROM executions
            WHERE order_id IS NOT NULL
            GROUP BY order_id
        )
        SELECT
            o.order_id,
            o.symbol,
            o.action,
            o.quantity,
            o.limit_price,
            o.created_at,
            COALESCE(ls.status, 'Submitted') AS status,
            COALESCE(et.executed_shares, ls.filled, 0) AS filled,
            CASE
                WHEN et.executed_shares IS NOT NULL THEN o.quantity - et.executed_shares
                ELSE COALESCE(ls.remaining, o.quantity)
            END AS remaining
        FROM orders o
        JOIN runs r ON r.id = o.run_id
        LEFT JOIN latest_status ls ON ls.order_id = o.order_id
        LEFT JOIN execution_totals et ON et.order_id = o.order_id
        WHERE r.config_path = :config_path
          AND r.dry_run = 0
          AND o.order_id IS NOT NULL
          AND o.created_at >= :cutoff
          AND COALESCE(ls.status, 'Submitted') IN :working_statuses
        ORDER BY o.created_at DESC
        LIMIT :limit
        """
    ).bindparams(bindparam("working_statuses", expanding=True))

    excluded = {int(order_id) for order_id in exclude_order_ids if order_id is not None}
    rows: list[dict[str, Any]] = []
    try:
        with engine.connect() as conn:
            result = conn.execute(
                query,
                {
                    "config_path": config_path,
                    "cutoff": cutoff,
                    "working_statuses": working_statuses,
                    "limit": limit * 2,
                },
            )
            for row in result.mappings():
                order_id = row.get("order_id")
                remaining = float(row.get("remaining") or 0)
                if order_id in excluded or remaining <= 0:
                    continue
                rows.append(dict(row))
                if len(rows) >= limit:
                    break
    except Exception as exc:
        logger.warning("Failed to load submitted/unfilled orders: %s", exc)
        return []

    return rows


def _summary_value(summary: dict[str, Any], *keys: str) -> float:
    for key in keys:
        item = summary.get(key)
        if isinstance(item, dict):
            value = item.get("value")
        else:
            value = item
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _load_latest_account_status_snapshot() -> dict[str, Any] | None:
    if not config or not config_path or not config.runtime.database.enabled:
        return None

    from sqlalchemy import create_engine, text

    db_url = config.runtime.database.resolve_url(config_path)
    engine = create_engine(db_url, future=True)
    query = text(
        """
        SELECT r.id AS run_id, r.started_at, a.summary_json
        FROM account_snapshots a
        JOIN runs r ON r.id = a.run_id
        WHERE r.config_path = :config_path
          AND r.dry_run = 0
        ORDER BY r.started_at DESC
        LIMIT 1
        """
    )
    try:
        with engine.connect() as conn:
            row = conn.execute(query, {"config_path": config_path}).mappings().first()
    except Exception as exc:
        logger.warning("Failed to load latest account snapshot: %s", exc)
        return None
    if not row:
        return None

    try:
        summary = json.loads(str(row["summary_json"]))
    except (TypeError, ValueError) as exc:
        logger.warning("Failed to parse latest account snapshot: %s", exc)
        return None

    if not isinstance(summary, dict):
        return None

    return {
        "run_id": row["run_id"],
        "started_at": row["started_at"],
        "net_liq": _summary_value(summary, "NetLiquidation"),
        "cash": _summary_value(summary, "TotalCashValue"),
        "buying_power": _summary_value(summary, "BuyingPower"),
        "excess_liq": _summary_value(summary, "ExcessLiquidity"),
        "maint_margin": _summary_value(
            summary,
            "FullMaintMarginReq",
            "MaintMarginReq",
            "LookAheadMaintMarginReq",
        ),
        "accrued": _summary_value(summary, "AccruedCash"),
        "cushion": _summary_value(summary, "Cushion"),
    }


def _format_db_status_fallback(error: Exception) -> str:
    snapshot = _load_latest_account_status_snapshot()
    submitted_unfilled_orders = _load_submitted_unfilled_orders()
    error_text = html.escape(str(error))
    message = (
        "⚠️ <b>IBKR live connection unavailable</b>\n"
        f"• Live status failed: <code>{error_text}</code>\n"
        "• Showing latest local DB snapshot instead.\n\n"
    )

    if snapshot:
        message += _format_status_message(
            net_liq=float(snapshot["net_liq"]),
            cash=float(snapshot["cash"]),
            buying_power=float(snapshot["buying_power"]),
            excess_liq=float(snapshot["excess_liq"]),
            maint_margin=float(snapshot["maint_margin"]),
            accrued=float(snapshot["accrued"]),
            cushion=float(snapshot["cushion"]),
        )
        message += (
            "\n\n🗄 <b>Snapshot Source</b>\n"
            f"• Run: <code>{snapshot['run_id']}</code>\n"
            f"• Started: <code>{html.escape(str(snapshot['started_at'])[:19])}</code>"
        )
    else:
        message += "📊 <b>Account Risk Dashboard</b>\n• No local DB snapshot available."

    message += "\n\n📌 <b>Current Open Orders</b>\n"
    message += "• Broker live open orders unavailable while IBKR API is offline."

    message += "\n\n🕓 <b>Submitted / Unfilled Orders</b>\n"
    if submitted_unfilled_orders:
        message += "\n".join(
            _format_submitted_order(order) for order in submitted_unfilled_orders
        )
    else:
        message += "• No recently submitted unfilled orders in DB."
    return message


def _format_ai_position_diagnosis(
    net_liq: float,
    cash: float,
    buying_power: float,
    maint_margin: float,
    cushion: float,
    open_trades: list[Any],
    today_fills: list[Any],
) -> str:
    cash_pct = cash / net_liq * 100 if net_liq else 0
    margin_usage = maint_margin / net_liq * 100 if net_liq else 0
    buying_power_x = buying_power / net_liq if net_liq else 0
    pending_count = len(open_trades)
    fill_count = len(today_fills)

    if cushion >= 0.40 and cash_pct >= 10:
        stance = "🟢 防守餘裕充足"
        advice = "可維持策略運作；新增 short premium 仍建議分批、小張數，避免單一標的集中。"
    elif cushion >= 0.30:
        stance = "🟡 可交易但需控槓桿"
        advice = "優先等掛單結果，不建議同時加大新倉；若波動升高，先保留現金與 SGOV 緩衝。"
    else:
        stance = "🟠 風險偏高"
        advice = "暫停新增風險部位；優先降低裸露 delta、補現金或縮小 short option exposure。"

    return (
        "\n\n🤖 <b>AI Position Diagnosis</b>\n"
        f"• 判斷：<b>{stance}</b>\n"
        f"• 今日成交筆數：<b>{fill_count}</b>；目前掛單：<b>{pending_count}</b>\n"
        f"• Margin usage: <b>{margin_usage:.1f}%</b>；Buying Power: <b>{buying_power_x:.2f}× NAV</b>\n"
        f"• 建議：{advice}"
    )


def _format_stock_position(contract, qty, avg_cost, mkt_price, mkt_value, pnl, pnl_pct):
    symbol = html.escape(contract.symbol)
    qty_txt = f"{qty:.0f}" if float(qty).is_integer() else f"{qty:.1f}"
    return (
        f"  📈 <b>{symbol}</b> {qty_txt} sh | cost {_money(avg_cost)} | "
        f"mark {_money(mkt_price)} | value {_compact_money(mkt_value)} | "
        f"PnL <b>{_money(pnl)}</b> [{_pct(pnl_pct)}]"
    )


def _format_option_position(contract, qty, avg_cost, mkt_price, mkt_value, pnl, pnl_pct):
    from thetagang.options import option_dte

    symbol = html.escape(contract.symbol)
    dte = option_dte(contract.lastTradeDateOrContractMonth)
    expiry = contract.lastTradeDateOrContractMonth
    if len(expiry) == 8:
        expiry = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
    side = "LONG" if qty > 0 else "SHORT"
    side_emoji = "🟢" if qty > 0 else "🔴"
    right = "CALL" if contract.right == "C" else "PUT"
    qty_txt = f"{abs(qty):.0f}" if float(abs(qty)).is_integer() else f"{abs(qty):.1f}"
    return (
        f"  {side_emoji} <b>{side} {right}</b> {symbol} {expiry} ${contract.strike:g} "
        f"×{qty_txt} | DTE {dte}\n"
        f"     cost {_money(avg_cost / 100)} | mark {_money(mkt_price)} | "
        f"value {_compact_money(mkt_value)} | PnL <b>{_money(pnl)}</b> [{_pct(pnl_pct)}] "
        f"| <code>{contract.conId}</code>"
    )


def _parse_option_expiry(expiry: str) -> Optional[date]:
    if not expiry:
        return None
    try:
        return datetime.strptime(str(expiry)[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _option_expiry_month(contract: Any, item: Any = None) -> Optional[str]:
    raw_expiry = getattr(contract, "lastTradeDateOrContractMonth", None)
    if not raw_expiry and item is not None:
        raw_expiry = getattr(item, "expiry", None)
    expiry_date = _parse_option_expiry(str(raw_expiry or ""))
    return expiry_date.strftime("%Y-%m") if expiry_date else None


def _option_multiplier(contract: Any) -> float:
    multiplier = getattr(contract, "multiplier", None) or 100
    try:
        return float(multiplier)
    except (TypeError, ValueError):
        return 100.0


def _is_option_contract(contract: Any) -> bool:
    return getattr(contract, "secType", "") in {"OPT", "FOP"}


def _execution_datetime(fill: Any, execution: Any) -> datetime:
    raw = getattr(fill, "time", None) or getattr(execution, "time", None)
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        for fmt in ("%Y%m%d  %H:%M:%S", "%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
    return datetime.now()


def _option_fill_cashflow(fill: Any) -> Optional[tuple[str, str, float, float, str]]:
    """Return (YYYY-MM, symbol, premium cashflow, contracts, side) for option fill.

    Positive cashflow = premium received (SLD); negative = premium paid (BOT).
    This is execution premium cashflow, not broker tax-lot realized P&L.
    """
    contract = getattr(fill, "contract", None)
    execution = getattr(fill, "execution", None)
    if not contract or not execution or not _is_option_contract(contract):
        return None

    side = getattr(execution, "side", "")
    sign = 1.0 if side == "SLD" else -1.0 if side == "BOT" else 0.0
    shares = float(getattr(execution, "shares", 0) or 0)
    price = float(getattr(execution, "price", 0) or 0)
    cashflow = sign * shares * price * _option_multiplier(contract)
    return (
        _execution_datetime(fill, execution).strftime("%Y-%m"),
        getattr(contract, "symbol", "-"),
        cashflow,
        shares,
        side,
    )


def _short_option_remaining_premium(item: Any) -> Optional[tuple[str, str, float]]:
    contract = getattr(item, "contract", None)
    position = float(getattr(item, "position", 0) or 0)
    if not contract or not _is_option_contract(contract) or position >= 0:
        return None
    expiry_date = _parse_option_expiry(getattr(contract, "lastTradeDateOrContractMonth", ""))
    if not expiry_date:
        return None
    remaining = max(0.0, -float(getattr(item, "marketValue", 0) or 0))
    return expiry_date.strftime("%Y-%m"), getattr(contract, "symbol", "-"), remaining


def _execution_record_cashflow(execution_record: Any) -> Optional[tuple[str, str, float, float, str]]:
    """Return (YYYY-MM, symbol, premium cashflow, contracts, side).

    Legacy imported IBKR rows did not persist sec_type/expiry/strike/right.  For
    those rows, keep short-option premium cashflows but exclude obvious stock
    rows and long LEAPS/capital-position debits, otherwise /revenue either shows
    only June live-sync data or mixes capital purchases into premium income.
    """
    sec_type = getattr(execution_record, "sec_type", None)
    side = getattr(execution_record, "side", "")
    shares = float(getattr(execution_record, "shares", 0) or 0)
    price = float(getattr(execution_record, "price", 0) or 0)

    if sec_type and sec_type != "OPT":
        return None
    if sec_type is None:
        exec_id = str(getattr(execution_record, "exec_id", "") or "")
        symbol = str(getattr(execution_record, "symbol", "") or "")
        configured_symbols = set(config.portfolio.symbols) if config else set()
        if not exec_id.startswith("import-") or symbol not in configured_symbols:
            return None
        # Legacy long LEAPS/stock-like debits were imported without contract
        # metadata. They are capital positions, not short-premium revenue.
        if side == "BOT" and price >= 50.0:
            return None

    sign = 1.0 if side == "SLD" else -1.0 if side == "BOT" else 0.0
    execution_time = getattr(execution_record, "execution_time", None)
    if not execution_time:
        return None
    if isinstance(execution_time, str):
        execution_time = datetime.fromisoformat(execution_time.replace("Z", "+00:00"))
    symbol = getattr(execution_record, "symbol", "-") or "-"
    return execution_time.strftime("%Y-%m"), symbol, sign * shares * price * 100.0, shares, side


def _format_revenue_message(
    fills: list[Any],
    portfolio: list[Any],
    today: Optional[date] = None,
    data_source: str = "IB Gateway current executions",
    synced_count: Optional[int] = None,
) -> str:
    today = today or date.today()
    revenue_start = _revenue_start_datetime()
    revenue_start_month = revenue_start.strftime("%Y-%m")

    records: list[dict[str, Any]] = []
    option_fill_count = 0
    for fill in fills:
        row = _execution_record_cashflow(fill) if hasattr(fill, "exec_id") else _option_fill_cashflow(fill)
        if not row:
            continue
        month, symbol, cashflow, contracts, side = row
        if month < revenue_start_month:
            continue
        records.append(
            {
                "month": month,
                "symbol": symbol,
                "cashflow": cashflow,
                "contracts": contracts,
                "side": side,
            }
        )
        option_fill_count += 1

    open_contracts_by_symbol: dict[str, float] = defaultdict(float)
    open_lots_by_symbol: dict[str, list[dict[str, float | str | None]]] = defaultdict(list)
    for item in portfolio:
        contract = getattr(item, "contract", None)
        position = float(getattr(item, "position", 0) or 0)
        if contract and _is_option_contract(contract) and position < 0:
            symbol = getattr(contract, "symbol", "-") or "-"
            contracts_open = abs(position)
            open_contracts_by_symbol[symbol] += contracts_open
            avg_cost = getattr(item, "averageCost", None)
            if avg_cost is None:
                avg_cost = getattr(item, "avg_cost", None)
            cost_basis = abs(float(avg_cost)) * contracts_open if avg_cost is not None else 0.0
            open_lots_by_symbol[symbol].append(
                {
                    "remaining": contracts_open,
                    "cost_basis": cost_basis,
                    "expiry_month": _option_expiry_month(contract, item),
                    "raw_cashflow": 0.0,
                }
            )
    for lots in open_lots_by_symbol.values():
        lots.sort(key=lambda lot: str(lot.get("expiry_month") or "9999-99"), reverse=True)

    open_row_ids: set[int] = set()
    open_row_lot: dict[int, dict[str, float | str | None]] = {}
    open_row_month: dict[int, str] = {}
    for idx in range(len(records) - 1, -1, -1):
        record = records[idx]
        symbol = record["symbol"]
        if record["side"] != "SLD":
            continue
        lot = next((lot for lot in open_lots_by_symbol.get(symbol, []) if float(lot["remaining"] or 0) > 0), None)
        if lot is None:
            continue
        open_row_ids.add(idx)
        open_row_lot[idx] = lot
        open_row_month[idx] = str(lot.get("expiry_month") or record["month"])
        lot["remaining"] = max(0.0, float(lot["remaining"] or 0) - float(record["contracts"] or 0))
        lot["raw_cashflow"] = float(lot["raw_cashflow"] or 0) + abs(float(record["cashflow"]))

    realized_by_month: dict[str, float] = defaultdict(float)
    unrealized_by_month: dict[str, float] = defaultdict(float)
    realized_by_symbol: dict[str, float] = defaultdict(float)
    unrealized_by_symbol: dict[str, float] = defaultdict(float)
    for idx, record in enumerate(records):
        month = record["month"]
        symbol = record["symbol"]
        cashflow = float(record["cashflow"])
        if idx in open_row_ids:
            lot = open_row_lot[idx]
            raw_cashflow = float(lot.get("raw_cashflow") or 0)
            cost_basis = float(lot.get("cost_basis") or 0)
            scale = cost_basis / raw_cashflow if cost_basis > 0 and raw_cashflow > 0 else 1.0
            pending_cashflow = cashflow * scale
            pending_month = open_row_month[idx]
            unrealized_by_month[pending_month] += pending_cashflow
            unrealized_by_symbol[symbol] += pending_cashflow
        else:
            realized_by_month[month] += cashflow
            realized_by_symbol[symbol] += cashflow

    months = sorted(set(realized_by_month) | set(unrealized_by_month))
    realized_total = sum(realized_by_month.values())
    unrealized_total = sum(unrealized_by_month.values())

    revenue_start_label = revenue_start.date().isoformat()
    msg = "💵 <b>Option Revenue Dashboard</b>\n\n"
    msg += f"📚 Source: <b>{html.escape(data_source)}</b>\n"
    msg += f"🗓 Revenue start: <b>{revenue_start_label}</b>\n"
    if synced_count is not None:
        msg += f"🔄 IBKR incremental sync returned: <b>{synced_count}</b> option fills\n"
    msg += "ℹ️ 已平倉=確認收益；未平倉=按到期月份列為待結算，不認列收益。\n"
    msg += "⚠️ Legacy rows use IBKR execution import + current short-option positions; commissions are included only where IBKR cost basis reflected them.\n\n"

    msg += "📆 <b>Monthly Premium Ledger</b>\n"
    if months:
        realized_month_count = len(realized_by_month) or 1
        average_realized = realized_total / realized_month_count
        msg += "<pre>Month    Realized     Open/Pending\n"
        for month in months:
            realized = realized_by_month[month]
            pending = unrealized_by_month[month]
            msg += f"{month} {realized:>11,.2f} {pending:>14,.2f}\n"
        msg += "</pre>"
        msg += f"\n✅ <b>Realized Total:</b> {_money(realized_total)}\n"
        msg += f"📈 <b>Realized Avg/mo:</b> {_money(average_realized)}\n"
    else:
        msg += "• No option executions found in the configured revenue window.\n"

    msg += f"\n⏳ <b>Open / Not Recognized Premium:</b> {_money(unrealized_total)}\n"
    msg += f"• Option fills counted: {option_fill_count}\n"
    msg += f"• Open short options counted: {sum(open_contracts_by_symbol.values()):.0f}\n\n"

    if realized_by_symbol:
        msg += "🏷 <b>Top Confirmed Symbols</b>\n"
        for symbol, amount in sorted(realized_by_symbol.items(), key=lambda x: abs(x[1]), reverse=True)[:5]:
            msg += f"• {html.escape(symbol)}: <b>{_money(amount)}</b>\n"
        msg += "\n"

    if unrealized_by_symbol:
        msg += "🔒 <b>Open / Pending Symbols</b>\n"
        for symbol, amount in sorted(unrealized_by_symbol.items(), key=lambda x: x[1], reverse=True)[:5]:
            msg += f"• {html.escape(symbol)}: <b>{_money(amount)}</b>\n"

    return msg.rstrip()


def _revenue_start_datetime() -> datetime:
    start_date = config.telegram.revenue_start_date if config else None
    if start_date:
        return datetime.fromisoformat(start_date)
    return datetime(date.today().year, 1, 1)


def _load_ytd_execution_records() -> list[Any]:
    if not config or not config_path or not config.runtime.database.enabled:
        return []

    from sqlalchemy import create_engine, text

    db_url = config.runtime.database.resolve_url(config_path)
    engine = create_engine(db_url, future=True)
    revenue_start = _revenue_start_datetime()
    query = text(
        """
        SELECT
            e.exec_id,
            e.order_id,
            e.order_ref,
            e.symbol,
            e.side,
            e.shares,
            e.price,
            e.execution_time,
            e.exchange,
            o.sec_type
        FROM executions e
        LEFT JOIN orders o ON o.order_id = e.order_id
        WHERE e.execution_time >= :revenue_start
          AND (
            o.sec_type = 'OPT'
            OR (o.sec_type IS NULL AND e.exec_id LIKE 'import-%')
          )
        ORDER BY e.execution_time ASC
        """
    )

    with engine.connect() as conn:
        rows = conn.execute(query, {"revenue_start": revenue_start}).mappings().all()
    return [SimpleNamespace(**dict(row)) for row in rows]


def _load_latest_position_snapshot() -> list[Any]:
    if not config or not config_path or not config.runtime.database.enabled:
        return []

    from sqlalchemy import create_engine, text

    db_url = config.runtime.database.resolve_url(config_path)
    engine = create_engine(db_url, future=True)
    query = text(
        """
        WITH latest_run AS (
            SELECT MAX(p.run_id) AS run_id
            FROM position_snapshots p
            JOIN runs r ON r.id = p.run_id
            WHERE r.config_path = :config_path
              AND r.dry_run = 0
        )
        SELECT
            p.symbol,
            p.con_id,
            p.sec_type,
            p.position,
            p.avg_cost,
            p.currency,
            p.exchange,
            p.multiplier,
            p.expiry,
            p.strike,
            p.right
        FROM position_snapshots p
        JOIN latest_run lr ON lr.run_id = p.run_id
        WHERE p.position IS NOT NULL
        ORDER BY p.symbol ASC
        """
    )

    with engine.connect() as conn:
        rows = conn.execute(query, {"config_path": str(config_path)}).mappings().all()

    positions = []
    for row in rows:
        contract = SimpleNamespace(
            conId=row["con_id"],
            symbol=row["symbol"],
            secType=row["sec_type"],
            currency=row["currency"],
            exchange=row["exchange"],
            multiplier=row["multiplier"],
            lastTradeDateOrContractMonth=row["expiry"],
            strike=row["strike"],
            right=row["right"],
        )
        positions.append(
            SimpleNamespace(
                contract=contract,
                position=row["position"],
                averageCost=row["avg_cost"],
            )
        )
    return positions


def _format_revenue_db_fallback(error: Exception, historical_records: list[Any]) -> str:
    if not historical_records:
        return f"Error fetching option revenue: {error}"

    portfolio = _load_latest_position_snapshot()
    msg = "⚠️ <b>IBKR live connection unavailable</b>\n"
    msg += "📚 Showing latest local DB revenue snapshot instead.\n"
    msg += f"Reason: <code>{html.escape(str(error))}</code>\n"
    if portfolio:
        msg += "🔒 Open/Pending uses the latest stored position snapshot, not live IBKR.\n\n"
    else:
        msg += "⚠️ No stored position snapshot found; Open/Pending may be understated.\n\n"
    msg += _format_revenue_message(
        fills=historical_records,
        portfolio=portfolio,
        data_source="local historical DB snapshot (IBKR live unavailable)",
        synced_count=None,
    )
    return msg


def _record_ibkr_execution_fills(fills: list[Any]) -> None:
    if not fills or not config or not config_path or not config.runtime.database.enabled:
        return

    from thetagang.db import DataStore

    db_url = config.runtime.database.resolve_url(config_path)
    raw_config = Path(config_path).read_text(encoding="utf-8")
    data_store = DataStore(db_url, config_path, dry_run=True, config_text=raw_config)
    data_store.record_executions(fills)


def _fill_monitor_state_path() -> Path:
    if config and config_path:
        db_path = Path(config.runtime.database.path)
        if not db_path.is_absolute():
            db_path = Path(config_path).parent / db_path
        return db_path.parent / "telegram_fill_monitor_state.json"
    return _strategy_state_dir() / "telegram_fill_monitor_state.json"


def _load_fill_monitor_state() -> dict[str, Any]:
    path = _fill_monitor_state_path()
    if not path.exists():
        return {"notified_exec_ids": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to load fill monitor state from %s", path)
        return {"notified_exec_ids": []}
    notified = data.get("notified_exec_ids", [])
    if not isinstance(notified, list):
        notified = []
    return {"notified_exec_ids": [str(exec_id) for exec_id in notified]}


def _save_fill_monitor_state(state: dict[str, Any]) -> None:
    path = _fill_monitor_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    notified = [str(exec_id) for exec_id in state.get("notified_exec_ids", [])]
    path.write_text(
        json.dumps({"notified_exec_ids": notified[-500:]}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _taipei_today_utc_window(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    now_tw = (now or datetime.now(TAIPEI_TZ)).astimezone(TAIPEI_TZ)
    start_tw = datetime.combine(now_tw.date(), datetime.min.time(), tzinfo=TAIPEI_TZ)
    end_tw = start_tw + timedelta(days=1)
    return (
        start_tw.astimezone(timezone.utc).replace(tzinfo=None),
        end_tw.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _load_unnotified_today_order_fills(
    notified_exec_ids: Sequence[str],
) -> list[dict[str, Any]]:
    if not config or not config_path or not config.runtime.database.enabled:
        return []

    from sqlalchemy import create_engine, text

    start_utc, end_utc = _taipei_today_utc_window()
    db_url = config.runtime.database.resolve_url(config_path)
    engine = create_engine(db_url, future=True)
    notified = {str(exec_id) for exec_id in notified_exec_ids}
    query = text(
        """
        SELECT
            e.exec_id,
            e.order_id,
            e.symbol,
            e.side,
            e.shares,
            e.price,
            e.execution_time,
            e.exchange,
            o.action AS order_action,
            o.quantity AS order_quantity,
            o.limit_price AS order_limit_price,
            o.sec_type AS order_sec_type,
            o.created_at AS order_created_at
        FROM executions e
        JOIN orders o ON o.order_id = e.order_id
        JOIN runs r ON r.id = o.run_id
        WHERE r.config_path = :config_path
          AND r.dry_run = 0
          AND o.created_at >= :start_utc
          AND o.created_at < :end_utc
          AND e.exec_id IS NOT NULL
        ORDER BY e.execution_time ASC, e.id ASC
        """
    )
    rows: list[dict[str, Any]] = []
    try:
        with engine.connect() as conn:
            result = conn.execute(
                query,
                {"config_path": config_path, "start_utc": start_utc, "end_utc": end_utc},
            )
            for row in result.mappings():
                if str(row.get("exec_id")) not in notified:
                    rows.append(dict(row))
    except Exception as exc:
        logger.warning("Failed to load unnotified order fills: %s", exc)
        return []
    return rows


def _format_fill_monitor_row(row: dict[str, Any]) -> str:
    symbol = html.escape(str(row.get("symbol") or "-"))
    side = html.escape(str(row.get("side") or "-"))
    shares = float(row.get("shares") or 0)
    price = float(row.get("price") or 0)
    sec_type = str(row.get("order_sec_type") or "")
    order_id = html.escape(str(row.get("order_id") or "-"))
    raw_time = row.get("execution_time")
    if isinstance(raw_time, str):
        exec_dt = _execution_datetime(SimpleNamespace(time=raw_time), SimpleNamespace())
    elif isinstance(raw_time, datetime):
        exec_dt = raw_time
    else:
        exec_dt = datetime.now(timezone.utc).replace(tzinfo=None)
    if exec_dt.tzinfo is None:
        exec_dt = exec_dt.replace(tzinfo=timezone.utc)
    when = exec_dt.astimezone(TAIPEI_TZ).strftime("%m-%d %H:%M")
    multiplier = 100 if sec_type == "OPT" else 1
    signed_cash = shares * price * multiplier
    if side == "BOT":
        signed_cash *= -1
    cash_label = "premium" if sec_type == "OPT" else "cash"
    return (
        f"• <code>{when} TW</code> {side} <b>{shares:g} {symbol}</b> "
        f"@ {_money(price)} — {cash_label} {_money(signed_cash)} "
        f"(order {order_id})"
    )


def _format_fill_monitor_message(rows: Sequence[dict[str, Any]]) -> str:
    lines = "\n".join(_format_fill_monitor_row(row) for row in rows)
    return "✅ <b>ThetaGang 當日下單已成交</b>\n\n" + lines


async def _sync_today_executions_from_ibkr() -> int:
    if not config:
        return 0
    from ib_async import ExecutionFilter

    ib = await get_ib_connection()
    try:
        today_filter = ExecutionFilter(
            acctCode=config.runtime.account.number,
            time=f"{datetime.now(TAIPEI_TZ):%Y%m%d} 00:00:00",
        )
        fills = await ib.reqExecutionsAsync(today_filter)
        _record_ibkr_execution_fills(fills)
        return len(fills)
    finally:
        ib.disconnect()


async def check_today_order_fills_once(application: Application) -> int:
    """Sync today's IBKR executions and push Telegram once per new exec_id."""
    if not config or not config.telegram.chat_id:
        return 0
    try:
        await _sync_today_executions_from_ibkr()
    except Exception as exc:
        logger.warning("IBKR execution sync failed; checking local DB fills only: %s", exc)
    try:
        state = _load_fill_monitor_state()
        notified = [str(exec_id) for exec_id in state.get("notified_exec_ids", [])]
        rows = _load_unnotified_today_order_fills(notified)
        if not rows:
            return 0
        await asyncio.wait_for(
            application.bot.send_message(
                chat_id=config.telegram.chat_id,
                text=_format_fill_monitor_message(rows),
                parse_mode="HTML",
            ),
            timeout=30,
        )
        state["notified_exec_ids"] = notified + [str(row["exec_id"]) for row in rows]
        _save_fill_monitor_state(state)
        return len(rows)
    except Exception as exc:
        logger.warning("Today order fill monitor failed: %s", exc)
        return 0


async def _fill_monitor_loop(application: Application) -> None:
    logger.info(
        "Starting order fill monitor loop: every %s seconds",
        ORDER_FILL_MONITOR_INTERVAL_SECONDS,
    )
    while True:
        await check_today_order_fills_once(application)
        await asyncio.sleep(ORDER_FILL_MONITOR_INTERVAL_SECONDS)


async def start_background_tasks(application: Application) -> None:
    asyncio.create_task(_fill_monitor_loop(application))
    asyncio.create_task(register_bot_commands(application))


def is_authorized(chat_id: int) -> bool:
    if not config or not config.telegram.chat_id:
        return False
    return str(chat_id) == str(config.telegram.chat_id)


class IBKROfflineError(RuntimeError):
    pass


def _ibkr_err(e: Exception) -> str:
    """Return a user-friendly error string, with special handling for offline IBKR."""
    if isinstance(e, IBKROfflineError):
        return str(e)
    err = str(e)
    if any(k in err for k in ("Connect call failed", "ConnectionRefused", "[Errno 111]")):
        return "⚠️ TWS 連線中斷，請稍後 30 秒再試。"
    return f"Error: {html.escape(err)}"


async def get_ib_connection():
    if not config:
        raise RuntimeError("Config is not loaded")
    from ib_async import IB
    ib = IB()
    try:
        await ib.connectAsync(
            config.runtime.watchdog.host,
            config.runtime.watchdog.port,
            clientId=99,
            timeout=10,
        )
    except Exception as exc:
        err = str(exc)
        if any(k in err for k in ("Connect call failed", "ConnectionRefused", "[Errno 111]", "timed out")):
            raise IBKROfflineError(
                "⚠️ TWS 連線中斷，請稍後 30 秒再試。"
            ) from exc
        raise
    return ib


async def zero_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Minimal help — only the most essential monitoring commands."""
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    msg = (
        "🤖 <b>ThetaGang — 快速導覽</b>\n\n"

        "📊 <b>帳戶狀態</b>\n"
        "/status — 帳戶風險儀表板（NAV / margin / cushion）\n"
        "/positions — 所有開倉部位\n\n"

        "💰 <b>績效</b>\n"
        "/pnl — Realized premium：今日 / 本週 / 本月 / YTD\n"
        "/theta — 各部位每日 theta decay 金額\n"
        "/expirations — 即將到期合約（DTE 警示）\n\n"

        "🎮 <b>操控</b>\n"
        "/pause &lt;symbol|all&gt; — 暫停交易\n"
        "/resume &lt;symbol|all&gt; — 恢復交易\n\n"

        "📖 /start — 完整命令列表"
    )
    await update.message.reply_html(msg)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    msg = (
        "🤖 <b>ThetaGang Telegram Bot</b>\n\n"

        "📊 <b>帳戶監控</b>\n"
        "/status — 帳戶風險儀表板（NAV / margin / cushion）\n"
        "/positions — 所有開倉部位（股票 + 選擇權）\n\n"

        "🔄 <b>交易記錄</b>\n"
        "/trades — 最近 3 天成交紀錄\n"
        "/orders — 即時掛單詳細狀態\n\n"

        "💰 <b>績效分析</b>\n"
        "/revenue — 選擇權 premium 收入（月報 + 未來 3 個月預估）\n"
        "/pnl — Realized premium：今日 / 本週 / 本月 / YTD\n"
        "/theta — 各部位每日 theta decay 金額 ✱\n"
        "/expirations — 即將到期合約（DTE 警示）\n\n"

        "📋 <b>執行歷史</b>\n"
        "/history [N] — 最近 N 次 trading engine 執行紀錄\n"
        "/events [symbol] — 最近決策事件（下單 / 啟動 / 結束）\n\n"

        "🔬 <b>進階分析</b>\n"
        "/greeks — Portfolio 全希臘值（Δ/Γ/Θ/V）+ 各標的分解 ✱\n"
        "/iv &lt;symbol&gt; — 個股 IV + 52 週 IV Rank / Percentile ✱\n"
        "/attribution — P&amp;L 歸因（Put/Call premium / Roll / 股票）\n"
        "/whatif &lt;symbol&gt; — 模擬平倉對保證金的影響（不下單） ✱\n"
        "/leaps &lt;symbol&gt; — PMCC LEAPS Call 建議（delta 0.70–0.80） ✱\n"
        "/buy_leaps &lt;symbol&gt; &lt;YYYYMMDD&gt; &lt;strike&gt; — 掛 LEAPS Call 限價買單 ✱\n\n"

        "⚙️ <b>策略設定</b>\n"
        "/strategy — 各標的權重與暫停狀態\n"
        "/settings — Margin / delta / SGOV / VIX hedge 參數\n\n"

        "🎮 <b>操控</b>\n"
        "/pause &lt;symbol|all&gt; — 暫停某標的或全局交易\n"
        "/resume &lt;symbol|all&gt; — 恢復交易\n"
        "/close &lt;conId|symbol&gt; — 手動市價平倉\n\n"

        "🛠 <b>Config 管理</b>\n"
        "/set_weight &lt;symbol&gt; &lt;%&gt; — 草稿修改目標權重\n"
        "/set_no_trading &lt;symbol&gt; &lt;true|false&gt; — 草稿封鎖某標的\n"
        "/preview_config — 查看 pending 草稿 diff\n"
        "/apply_config — 套用草稿到正式 config\n"
        "/discard_config — 丟棄草稿\n"
        "/reload_strategy — 重新載入 TOML 到 Telegram daemon\n\n"

        "<i>✱ 需要市場開盤（即時行情）</i>"
    )
    await update.message.reply_html(msg)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config:
        await update.message.reply_text("Error: Config not loaded.")
        return

    status_msg = await update.message.reply_text("Connecting to IBKR for status update...")
    try:
        ib = await get_ib_connection()
        account_summary = await ib.accountSummaryAsync(config.runtime.account.number)

        from ib_async import ExecutionFilter
        from thetagang.util import account_summary_to_dict
        acct_dict = account_summary_to_dict(account_summary)
        
        net_liq = float(acct_dict.get("NetLiquidation", {}).value or 0)
        excess_liq = float(acct_dict.get("ExcessLiquidity", {}).value or 0)
        maint_margin = float(acct_dict.get("FullMaintMarginReq", {}).value or 0)
        cash = float(acct_dict.get("TotalCashValue", {}).value or 0)
        cushion = float(acct_dict.get("Cushion", {}).value or 0)
        buying_power = float(acct_dict.get("BuyingPower", {}).value or 0)
        accrued = float(acct_dict.get("AccruedCash", {}).value or 0)

        today_filter = ExecutionFilter(
            acctCode=config.runtime.account.number,
            time=f"{date.today():%Y%m%d} 00:00:00",
        )
        today_fills = await ib.reqExecutionsAsync(today_filter)
        _record_ibkr_execution_fills(today_fills)
        open_trades = ib.openTrades()

        ib.disconnect()

        message = _format_status_message(
            net_liq=net_liq,
            cash=cash,
            buying_power=buying_power,
            excess_liq=excess_liq,
            maint_margin=maint_margin,
            accrued=accrued,
            cushion=cushion,
        )
        message += "\n\n🧾 <b>Today Executions</b>\n"
        if today_fills:
            message += "\n".join(_format_today_fill(fill) for fill in today_fills[-5:])
        else:
            message += "• No executions since local midnight."

        message += "\n\n📌 <b>Current Open Orders</b>\n"
        if open_trades:
            message += "\n".join(_format_open_trade(trade) for trade in open_trades[:5])
        else:
            message += "• No live broker open orders."

        message += _format_ai_position_diagnosis(
            net_liq=net_liq,
            cash=cash,
            buying_power=buying_power,
            maint_margin=maint_margin,
            cushion=cushion,
            open_trades=open_trades,
            today_fills=today_fills,
        )
        await status_msg.edit_text(message, parse_mode="HTML")
    except Exception as e:
        logger.warning("Live IBKR status failed; falling back to DB snapshot: %s", e)
        await status_msg.edit_text(_format_db_status_fallback(e), parse_mode="HTML")


async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config:
        await update.message.reply_text("Error: Config not loaded.")
        return

    status_msg = await update.message.reply_text("Fetching open positions...")
    try:
        from ib_async import Option, Stock

        ib = await get_ib_connection()
        portfolio = ib.portfolio(config.runtime.account.number)

        # Sync today's fills to DB while connection is open
        try:
            from ib_async import ExecutionFilter
            today_filter = ExecutionFilter(
                acctCode=config.runtime.account.number,
                time=f"{date.today():%Y%m%d} 00:00:00",
            )
            today_fills = await ib.reqExecutionsAsync(today_filter)
            _record_ibkr_execution_fills(today_fills)
        except Exception:
            pass

        grouped_positions = {}
        stock_value = 0.0
        option_value = 0.0
        total_pnl = 0.0

        for item in portfolio:
            if item.position == 0:
                continue
            contract = item.contract
            qty = item.position
            mkt_price = item.marketPrice
            avg_cost = item.averageCost
            mkt_value = item.marketValue
            unrealized_pnl = item.unrealizedPNL
            pnl_pct = (unrealized_pnl / (avg_cost * abs(qty))) * 100 if avg_cost > 0 else 0
            symbol = contract.symbol
            grouped_positions.setdefault(symbol, {"stocks": [], "options": []})
            total_pnl += unrealized_pnl

            if isinstance(contract, Stock):
                stock_value += mkt_value
                grouped_positions[symbol]["stocks"].append(
                    _format_stock_position(
                        contract,
                        qty,
                        avg_cost,
                        mkt_price,
                        mkt_value,
                        unrealized_pnl,
                        pnl_pct,
                    )
                )
            elif isinstance(contract, Option):
                option_value += mkt_value
                grouped_positions[symbol]["options"].append(
                    _format_option_position(
                        contract,
                        qty,
                        avg_cost,
                        mkt_price,
                        mkt_value,
                        unrealized_pnl,
                        pnl_pct,
                    )
                )

        ib.disconnect()

        msg = "📦 <b>Open Positions by Underlying</b>\n\n"
        msg += "📌 <b>Summary</b>\n"
        msg += f"• Stock/ETF value: <b>{_compact_money(stock_value)}</b>\n"
        msg += f"• Option net value: <b>{_compact_money(option_value)}</b>\n"
        msg += f"• Open PnL: <b>{_money(total_pnl)}</b>\n\n"

        if grouped_positions:
            for symbol in sorted(grouped_positions):
                group = grouped_positions[symbol]
                msg += f"<b>[{html.escape(symbol)}]</b>\n"
                lines = group["stocks"] + group["options"]
                msg += "\n".join(lines) + "\n\n"
        else:
            msg += "• No open positions."

        await status_msg.edit_text(msg, parse_mode="HTML")
    except Exception as e:
        await status_msg.edit_text(_ibkr_err(e), parse_mode="HTML")


async def trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config or not config_path:
        await update.message.reply_text("Error: Config not loaded.")
        return

    status_msg = await update.message.reply_text("Syncing trades from IBKR...")
    try:
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import sessionmaker
        from thetagang.db import ExecutionRecord

        # Sync last 3 days of fills from IBKR before reading DB
        try:
            from ib_async import ExecutionFilter
            ib = await get_ib_connection()
            three_days_ago = (datetime.now() - timedelta(days=3)).strftime("%Y%m%d 00:00:00")
            sync_filter = ExecutionFilter(
                acctCode=config.runtime.account.number,
                time=three_days_ago,
            )
            recent_fills = await ib.reqExecutionsAsync(sync_filter)
            _record_ibkr_execution_fills(recent_fills)
            ib.disconnect()
        except Exception:
            pass  # fall through to DB-only display

        db_url = config.runtime.database.resolve_url(config_path)
        engine = create_engine(db_url, future=True)
        Session = sessionmaker(bind=engine, future=True)

        cutoff = datetime.now() - timedelta(days=3)
        with Session() as session:
            stmt = (
                select(ExecutionRecord)
                .where(ExecutionRecord.execution_time >= cutoff)
                .order_by(ExecutionRecord.execution_time.desc())
            )
            executions = session.execute(stmt).scalars().all()
            
        if not executions:
            await status_msg.edit_text("No trade executions found in the last 3 days.")
            return
            
        msg = "🔄 <b>Recent Trades (Last 3 Days)</b>\n\n"
        for ex in executions:
            if ex.execution_time:
                dt_utc = ex.execution_time.replace(tzinfo=timezone.utc) if ex.execution_time.tzinfo is None else ex.execution_time
                dt_tw = dt_utc.astimezone(TAIPEI_TZ)
                time_str = dt_tw.strftime("%m-%d %H:%M") + " TW"
            else:
                time_str = "-"
            side_str = "🟢 BOT" if ex.side == "BOT" else "🔴 SLD"
            msg += f"• <code>{time_str}</code> | {side_str} <b>{abs(ex.shares):.1f} {ex.symbol}</b> @ ${ex.price:.2f} (Ref: {ex.order_ref or '-'})\n"
            
        await status_msg.edit_text(msg, parse_mode="HTML")
    except Exception as e:
        await status_msg.edit_text(f"Error querying trades: {e}")


async def revenue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config:
        await update.message.reply_text("Error: Config not loaded.")
        return

    status_msg = await update.message.reply_text("Loading option revenue from historical DB...")
    ib = None
    historical_records: list[Any] = []
    try:
        from ib_async import ExecutionFilter

        historical_records = _load_ytd_execution_records()
        synced_count = 0

        ib = await get_ib_connection()
        exec_filter = ExecutionFilter(
            acctCode=config.runtime.account.number,
            time=f"{date.today().year}0101 00:00:00",
            secType="OPT",
        )
        fills = await ib.reqExecutionsAsync(exec_filter)
        synced_count = len(fills)
        _record_ibkr_execution_fills(fills)
        historical_records = _load_ytd_execution_records()
        portfolio = ib.portfolio(config.runtime.account.number)

        if historical_records:
            data_source = "historical execution database + IBKR incremental sync"
            revenue_rows = historical_records
        else:
            data_source = "IBKR incremental sync only (no persisted history yet)"
            revenue_rows = fills

        message = _format_revenue_message(
            fills=revenue_rows,
            portfolio=portfolio,
            data_source=data_source,
            synced_count=synced_count,
        )
        ib.disconnect()
        await status_msg.edit_text(message, parse_mode="HTML")
    except Exception as e:
        if ib and ib.isConnected():
            ib.disconnect()
        message = _format_revenue_db_fallback(e, historical_records)
        await status_msg.edit_text(message, parse_mode="HTML")


def _strategy_state_dir() -> Path:
    if not config:
        return Path("data")
    return Path(config.runtime.database.path).parent


def _pause_state_path() -> Path:
    return _strategy_state_dir() / "telegram_bot_state.json"


def _config_draft_path() -> Path:
    return _strategy_state_dir() / "thetagang_config_draft.toml"


def _load_config_doc(path: Path | str) -> Any:
    return tomlkit.parse(Path(path).read_text(encoding="utf-8"))


def _current_config_doc() -> Any:
    if not config_path:
        raise RuntimeError("Config path is not loaded")
    return _load_config_doc(config_path)


def _draft_or_current_config_doc() -> Any:
    draft_path = _config_draft_path()
    if draft_path.exists():
        return _load_config_doc(draft_path)
    return _current_config_doc()


def _write_draft_config(doc: Any) -> Path:
    draft_path = _config_draft_path()
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return draft_path


def _validate_config_doc(doc: Any) -> Config:
    return Config(**doc.unwrap())


def _configured_symbols_from_doc(doc: Any) -> list[str]:
    return list(doc["portfolio"]["symbols"].keys())


def _parse_bool_arg(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"true", "1", "yes", "y", "on"}:
        return True
    if value in {"false", "0", "no", "n", "off"}:
        return False
    raise ValueError("Use true/false, yes/no, on/off, or 1/0")


def _render_config_diff() -> str:
    if not config_path:
        raise RuntimeError("Config path is not loaded")
    draft_path = _config_draft_path()
    if not draft_path.exists():
        return "No pending strategy config draft."
    current_lines = Path(config_path).read_text(encoding="utf-8").splitlines()
    draft_lines = draft_path.read_text(encoding="utf-8").splitlines()
    diff = list(difflib.unified_diff(
        current_lines,
        draft_lines,
        fromfile="thetagang.toml",
        tofile="pending-draft",
        lineterm="",
        n=3,
    ))
    if not diff:
        return "Pending draft exists but has no diff from current config."
    return "\n".join(diff)


async def _reply_long_html(update: Update, title: str, body: str) -> None:
    text = f"{title}\n<pre>{html.escape(body)}</pre>"
    if len(text) <= 3900:
        await update.message.reply_html(text)
        return
    await update.message.reply_html(title)
    for start in range(0, len(body), 3200):
        await update.message.reply_html(f"<pre>{html.escape(body[start:start+3200])}</pre>")


async def strategy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config:
        await update.message.reply_text("Error: Config not loaded.")
        return
        
    paused_all = False
    paused_symbols = []
    try:
        state_dir = Path(config.runtime.database.path).parent
        state_path = state_dir / "telegram_bot_state.json"
        if state_path.exists():
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            paused_all = state.get("paused_all", False)
            paused_symbols = state.get("paused_symbols", [])
    except Exception:
        pass
        
    msg = "📈 <b>Portfolio Strategy Settings</b>\n\n"
    msg += "Global Trading Status: "
    msg += "⏸ <b>PAUSED</b>\n\n" if paused_all else "▶️ <b>RUNNING</b>\n\n"
    
    msg += "<b>Configured Symbol Allocations:</b>\n"
    for symbol, sconfig in config.portfolio.symbols.items():
        is_paused = symbol in paused_symbols
        status_emoji = "⏸" if is_paused else "▶️"
        status_text = " (Paused)" if is_paused else ""
        msg += f"• {status_emoji} <b>{symbol}</b>: Weight: <b>{sconfig.weight * 100:.1f}%</b>{status_text}\n"
        
    await update.message.reply_html(msg)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config:
        await update.message.reply_text("Error: Config not loaded.")
        return

    target = config.strategies.wheel.defaults.target
    cash = config.strategies.cash_management
    vix = config.strategies.vix_call_hedge
    default_put_delta = (
        target.puts.delta if target.puts and target.puts.delta is not None else target.delta
    )
    default_call_delta = (
        target.calls.delta if target.calls and target.calls.delta is not None else target.delta
    )

    msg = "⚙️ <b>ThetaGang Strategy Settings</b>\n\n"
    msg += "<b>Risk / Deployment</b>\n"
    msg += f"• margin_usage: <b>{config.runtime.account.margin_usage:.2f}</b>\n"
    msg += f"• market_data_type: <b>{config.runtime.account.market_data_type}</b>\n"
    msg += f"• max new contracts/pass: <b>{target.maximum_new_contracts_percent * 100:.1f}%</b>\n\n"

    msg += "<b>Default Wheel Target</b>\n"
    msg += f"• DTE: <b>{target.dte}</b>, max DTE: <b>{target.max_dte}</b>\n"
    msg += f"• default delta: <b>{target.delta:.2f}</b>\n"
    msg += f"• put delta: <b>{default_put_delta:.2f}</b>\n"
    msg += f"• call delta: <b>{default_call_delta:.2f}</b>\n\n"

    msg += "<b>Symbols</b>\n"
    for symbol, sconfig in config.portfolio.symbols.items():
        put_delta = sconfig.puts.delta if sconfig.puts and sconfig.puts.delta is not None else default_put_delta
        call_delta = sconfig.calls.delta if sconfig.calls and sconfig.calls.delta is not None else default_call_delta
        msg += (
            f"• <b>{html.escape(symbol)}</b>: weight <b>{sconfig.weight * 100:.1f}%</b>, "
            f"put Δ <b>{put_delta:.2f}</b>, call Δ <b>{call_delta:.2f}</b>\n"
        )

    msg += "\n<b>Cash / SGOV</b>\n"
    msg += f"• enabled: <b>{cash.enabled}</b>\n"
    msg += f"• cash_fund: <b>{html.escape(cash.cash_fund)}</b>\n"
    msg += f"• target_cash_balance: <b>{_money(cash.target_cash_balance)}</b>\n"
    msg += f"• buy/sell threshold: <b>{_money(cash.buy_threshold)}</b> / <b>{_money(cash.sell_threshold)}</b>\n\n"

    msg += "<b>VIX Hedge</b>\n"
    msg += f"• enabled: <b>{vix.enabled}</b>\n"
    msg += f"• delta: <b>{vix.delta:.2f}</b>, target DTE: <b>{vix.target_dte}</b>\n"
    msg += "• allocation: " + ", ".join(
        f"{getattr(a, 'lower_bound', 0) or 0:g}-{getattr(a, 'upper_bound', '∞') or '∞'}: {a.weight * 100:.2f}%"
        for a in vix.allocation
    )

    await update.message.reply_html(msg)


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config:
        await update.message.reply_text("Error: Config not loaded.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /pause <symbol|all>")
        return
        
    target = context.args[0].upper()
    state_dir = Path(config.runtime.database.path).parent
    state_path = state_dir / "telegram_bot_state.json"
    
    state = {"paused_all": False, "paused_symbols": []}
    if state_path.exists():
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass
            
    if target == "ALL":
        state["paused_all"] = True
        msg = "⏸ <b>All trading has been paused globally.</b>"
    else:
        paused_symbols = state.setdefault("paused_symbols", [])
        if target not in paused_symbols:
            paused_symbols.append(target)
        msg = f"⏸ <b>Trading for {target} has been paused.</b>"
        
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        await update.message.reply_html(msg)
    except Exception as e:
        await update.message.reply_text(f"Error saving pause state: {e}")


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config:
        await update.message.reply_text("Error: Config not loaded.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /resume <symbol|all>")
        return
        
    target = context.args[0].upper()
    state_dir = Path(config.runtime.database.path).parent
    state_path = state_dir / "telegram_bot_state.json"
    
    state = {"paused_all": False, "paused_symbols": []}
    if state_path.exists():
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass
            
    if target == "ALL":
        state["paused_all"] = False
        msg = "▶️ <b>Global trading has been resumed.</b>"
    else:
        paused_symbols = state.get("paused_symbols", [])
        if target in paused_symbols:
            paused_symbols.remove(target)
        msg = f"▶️ <b>Trading for {target} has been resumed.</b>"
        
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        await update.message.reply_html(msg)
    except Exception as e:
        await update.message.reply_text(f"Error saving resume state: {e}")


async def set_weight_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config or not config_path:
        await update.message.reply_text("Error: Config not loaded.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /set_weight <symbol> <percent>  (example: /set_weight TSLA 60)")
        return
    symbol = context.args[0].upper()
    try:
        percent = float(context.args[1].replace("%", ""))
        if percent < 0 or percent > 100:
            raise ValueError("percent must be between 0 and 100")
        doc = _draft_or_current_config_doc()
        symbols = doc["portfolio"]["symbols"]
        if symbol not in symbols:
            await update.message.reply_text(f"Unknown symbol: {symbol}. Configured: {', '.join(_configured_symbols_from_doc(doc))}")
            return
        old_weight = float(symbols[symbol].get("weight", 0.0))
        symbols[symbol]["weight"] = percent / 100.0
        draft_path = _write_draft_config(doc)
        total = sum(float(s.get("weight", 0.0)) for s in symbols.values())
        msg = (
            f"📝 Pending draft updated: {symbol} weight {old_weight * 100:.1f}% → {percent:.1f}%\n"
            f"Total weight is now {total * 100:.1f}% (must be 100.0% before /apply_config).\n"
            f"Draft: {draft_path}\n"
            "Next: /preview_config then /apply_config"
        )
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Error updating draft weight: {e}")


async def set_no_trading_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config or not config_path:
        await update.message.reply_text("Error: Config not loaded.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /set_no_trading <symbol> <true|false>  (true = disable trading)")
        return
    symbol = context.args[0].upper()
    try:
        value = _parse_bool_arg(context.args[1])
        doc = _draft_or_current_config_doc()
        symbols = doc["portfolio"]["symbols"]
        if symbol not in symbols:
            await update.message.reply_text(f"Unknown symbol: {symbol}. Configured: {', '.join(_configured_symbols_from_doc(doc))}")
            return
        old_value = bool(symbols[symbol].get("no_trading", False))
        symbols[symbol]["no_trading"] = value
        _validate_config_doc(doc)
        draft_path = _write_draft_config(doc)
        await update.message.reply_text(
            f"📝 Pending draft updated: {symbol} no_trading {old_value} → {value}\n"
            f"Draft: {draft_path}\n"
            "Next: /preview_config then /apply_config"
        )
    except Exception as e:
        await update.message.reply_text(f"Error updating draft no_trading: {e}")


async def preview_config_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config or not config_path:
        await update.message.reply_text("Error: Config not loaded.")
        return
    try:
        diff = _render_config_diff()
        draft_path = _config_draft_path()
        validation = ""
        if draft_path.exists():
            try:
                draft_cfg = _validate_config_doc(_load_config_doc(draft_path))
                weights = ", ".join(f"{s}: {c.weight * 100:.1f}%" for s, c in draft_cfg.portfolio.symbols.items())
                validation = f"\n\nValidation: OK\nWeights: {weights}"
            except Exception as e:
                validation = f"\n\nValidation: FAILED — {e}"
        await _reply_long_html(update, "🔎 Pending strategy config diff", diff + validation)
    except Exception as e:
        await update.message.reply_text(f"Error previewing config: {e}")


async def apply_config_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global config
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config or not config_path:
        await update.message.reply_text("Error: Config not loaded.")
        return
    draft_path = _config_draft_path()
    if not draft_path.exists():
        await update.message.reply_text("No pending draft. Use /set_weight or /set_no_trading first.")
        return
    try:
        draft_doc = _load_config_doc(draft_path)
        new_config = _validate_config_doc(draft_doc)
        current_path = Path(config_path)
        backup_path = current_path.with_suffix(current_path.suffix + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(current_path, backup_path)
        current_path.write_text(tomlkit.dumps(draft_doc), encoding="utf-8")
        draft_path.unlink()
        config = new_config
        await update.message.reply_text(
            "✅ Strategy config applied to thetagang.toml and reloaded by Telegram daemon.\n"
            f"Backup: {backup_path}\n"
            "Note: live trading container must be restarted separately before a running strategy process uses the new TOML."
        )
    except Exception as e:
        await update.message.reply_text(f"Error applying config draft: {e}")


async def reload_strategy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global config
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config_path:
        await update.message.reply_text("Error: Config path not loaded.")
        return
    try:
        doc = _current_config_doc()
        config = _validate_config_doc(doc)
        await update.message.reply_text("✅ Telegram daemon reloaded thetagang.toml into memory. Live trading container is unchanged.")
    except Exception as e:
        await update.message.reply_text(f"Error reloading strategy config: {e}")


async def discard_config_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    draft_path = _config_draft_path()
    if draft_path.exists():
        draft_path.unlink()
        await update.message.reply_text("🗑 Pending strategy config draft discarded.")
    else:
        await update.message.reply_text("No pending draft to discard.")


async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config:
        await update.message.reply_text("Error: Config not loaded.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /close <conId|symbol>")
        return
        
    target = context.args[0]
    status_msg = await update.message.reply_text(f"Attempting to close position for {target}...")
    
    try:
        from ib_async import MarketOrder
        ib = await get_ib_connection()
        portfolio = ib.portfolio(config.runtime.account.number)
        
        target_item = None
        for item in portfolio:
            if item.position == 0:
                continue
            contract = item.contract
            if str(contract.conId) == target or contract.symbol.upper() == target.upper() or contract.localSymbol.upper() == target.upper():
                target_item = item
                break
                
        if not target_item:
            await status_msg.edit_text(f"Could not find open position matching: {target}")
            ib.disconnect()
            return
            
        contract = target_item.contract
        qty = target_item.position

        action = "SELL" if qty > 0 else "BUY"
        close_qty = abs(qty)

        # Use LimitOrder for options (IBKR rejects MarketOrder on illiquid/cheap options);
        # use MarketOrder for stocks/ETFs where market orders are reliable.
        is_option = getattr(contract, "secType", "") == "OPT"
        if is_option:
            from ib_async import LimitOrder, util
            # Fetch live bid/ask to set a realistic limit; portfolio mark can lag
            ticker = ib.reqMktData(contract, genericTickList="")
            await asyncio.sleep(3)
            bid = ticker.bid if ticker.bid and not util.isNan(ticker.bid) and ticker.bid > 0 else None
            ask = ticker.ask if ticker.ask and not util.isNan(ticker.ask) and ticker.ask > 0 else None
            mark = float(getattr(target_item, "marketPrice", 0) or 0)
            if action == "SELL":
                # Sell: use bid if available; fall back to half-ask or mark
                lmt_price = round(max(bid or (ask * 0.5 if ask else mark), 0.05), 2)
            else:
                # Buy: use ask if available; fall back to mark
                lmt_price = round(max(ask or mark, 0.05), 2)
            order = LimitOrder(action, close_qty, lmt_price)
            order.tif = "GTC"
            order_desc = f"Limit @ ${lmt_price:.2f} GTC"
        else:
            order = MarketOrder(action, close_qty)
            order_desc = "Market"

        trade = ib.placeOrder(contract, order)

        # Wait up to 10 seconds for order fill
        for _ in range(10):
            await asyncio.sleep(1)
            if trade.isDone():
                break

        status = trade.orderStatus.status
        avg_price = trade.orderStatus.avgFillPrice
        ib.disconnect()

        if trade.isDone():
            await status_msg.edit_text(
                f"✅ <b>Successfully closed position:</b>\n"
                f"• Contract: {contract.symbol} {contract.localSymbol}\n"
                f"• Order: {action} {close_qty} ({order_desc})\n"
                f"• Status: <b>Filled</b> @ ${avg_price:.2f}",
                parse_mode="HTML"
            )
        else:
            await status_msg.edit_text(
                f"⏳ <b>Close order submitted (working):</b>\n"
                f"• Contract: {contract.symbol} {contract.localSymbol}\n"
                f"• Order: {action} {close_qty} ({order_desc})\n"
                f"• Status: <b>{status}</b> — check /orders for fill",
                parse_mode="HTML"
            )
    except Exception as e:
        await status_msg.edit_text(_ibkr_err(e), parse_mode="HTML")


async def expirations_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config:
        await update.message.reply_text("Error: Config not loaded.")
        return

    status_msg = await update.message.reply_text("Fetching option expirations...")
    try:
        from ib_async import Option
        from thetagang.options import option_dte

        ib = await get_ib_connection()
        portfolio = ib.portfolio(config.runtime.account.number)
        ib.disconnect()

        options = [
            item for item in portfolio
            if isinstance(item.contract, Option) and item.position != 0
        ]

        if not options:
            await status_msg.edit_text("📅 No open option positions found.")
            return

        options.sort(key=lambda item: item.contract.lastTradeDateOrContractMonth)

        today = date.today()
        msg = "📅 <b>Upcoming Option Expirations</b>\n\n"

        def _dte_emoji(dte: int) -> str:
            if dte <= 3:
                return "🔴"
            if dte <= 7:
                return "🟠"
            if dte <= 14:
                return "🟡"
            return "✅"

        for item in options:
            contract = item.contract
            qty = item.position
            dte = option_dte(contract.lastTradeDateOrContractMonth)
            expiry_str = contract.lastTradeDateOrContractMonth
            if len(expiry_str) == 8:
                expiry_str = f"{expiry_str[:4]}-{expiry_str[4:6]}-{expiry_str[6:]}"
            side = "SHORT" if qty < 0 else "LONG"
            right = "CALL" if contract.right == "C" else "PUT"
            emoji = _dte_emoji(dte)
            abs_qty = int(abs(qty))
            msg += (
                f"{emoji} <b>{contract.symbol}</b> {side} {right} "
                f"${contract.strike:g} × {abs_qty} — "
                f"<code>{expiry_str}</code> [{dte} DTE]\n"
            )

        await status_msg.edit_text(msg, parse_mode="HTML")

    except Exception as e:
        fallback = _expirations_from_db()
        if fallback:
            await status_msg.edit_text(fallback, parse_mode="HTML")
        else:
            await status_msg.edit_text(f"Error fetching expirations: {html.escape(str(e))}")


def _expirations_from_db() -> Optional[str]:
    """Fallback: build expiration list from last position snapshot in DB."""
    if not config or not config_path or not config.runtime.database.enabled:
        return None
    try:
        from thetagang.options import option_dte
        positions = _load_latest_position_snapshot()
        options = [
            p for p in positions
            if getattr(p.contract, "secType", "") == "OPT" and p.position != 0
        ]
        if not options:
            return "📅 No option positions found in DB snapshot."
        options.sort(key=lambda p: p.contract.lastTradeDateOrContractMonth or "")

        def _dte_emoji(dte: int) -> str:
            if dte <= 3:
                return "🔴"
            if dte <= 7:
                return "🟠"
            if dte <= 14:
                return "🟡"
            return "✅"

        msg = "📅 <b>Upcoming Option Expirations</b> <i>(DB snapshot)</i>\n\n"
        for p in options:
            contract = p.contract
            dte = option_dte(contract.lastTradeDateOrContractMonth or "")
            expiry_str = str(contract.lastTradeDateOrContractMonth or "?")
            if len(expiry_str) == 8:
                expiry_str = f"{expiry_str[:4]}-{expiry_str[4:6]}-{expiry_str[6:]}"
            side = "SHORT" if p.position < 0 else "LONG"
            right = "CALL" if str(contract.right or "").startswith("C") else "PUT"
            emoji = _dte_emoji(dte)
            msg += (
                f"{emoji} <b>{html.escape(contract.symbol)}</b> {side} {right} "
                f"${contract.strike or 0:g} × {int(abs(p.position))} — "
                f"<code>{expiry_str}</code> [{dte} DTE]\n"
            )
        return msg
    except Exception as exc:
        logger.warning("DB expiration fallback failed: %s", exc)
        return None


async def pnl_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config or not config_path:
        await update.message.reply_text("Error: Config not loaded.")
        return

    status_msg = await update.message.reply_text("Calculating P&L from database...")
    try:
        from sqlalchemy import create_engine, text as sa_text

        db_url = config.runtime.database.resolve_url(config_path)
        engine = create_engine(db_url, future=True)

        now_taipei = datetime.now(TAIPEI_TZ)
        today_start = now_taipei.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())
        month_start = today_start.replace(day=1)
        ytd_start = _revenue_start_datetime().replace(tzinfo=TAIPEI_TZ) if _revenue_start_datetime().tzinfo else _revenue_start_datetime().replace(tzinfo=timezone.utc).astimezone(TAIPEI_TZ)

        # Convert to UTC for DB comparison (stored as UTC-naive)
        def _to_utc_naive(dt_taipei: datetime) -> datetime:
            return dt_taipei.astimezone(timezone.utc).replace(tzinfo=None)

        query = sa_text(
            """
            SELECT
                e.side,
                e.shares,
                e.price,
                e.execution_time,
                o.sec_type
            FROM executions e
            LEFT JOIN orders o ON o.order_id = e.order_id
            WHERE e.execution_time >= :ytd_start
              AND (
                o.sec_type = 'OPT'
                OR o.sec_type = 'BAG'
                OR (o.sec_type IS NULL AND e.exec_id LIKE 'import-%')
              )
            ORDER BY e.execution_time ASC
            """
        )

        with engine.connect() as conn:
            rows = list(conn.execute(query, {"ytd_start": _to_utc_naive(ytd_start)}).mappings())

        def _cashflow(row: Any) -> float:
            side = str(row.get("side") or "")
            shares = float(row.get("shares") or 0)
            price = float(row.get("price") or 0)
            sec_type = row.get("sec_type")
            # Imported rows (sec_type IS NULL) with high BOT price = stock/LEAPS capital positions
            if side == "BOT" and sec_type is None and price >= 50.0:
                return 0.0
            sign = 1.0 if side == "SLD" else -1.0
            return sign * shares * abs(price) * 100

        def _row_dt(row: Any) -> datetime:
            raw = row.get("execution_time")
            if isinstance(raw, datetime):
                dt = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
            else:
                try:
                    dt = datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except Exception:
                    dt = datetime.now(timezone.utc)
            return dt.astimezone(TAIPEI_TZ)

        today_pnl = sum(_cashflow(r) for r in rows if _row_dt(r) >= today_start)
        week_pnl = sum(_cashflow(r) for r in rows if _row_dt(r) >= week_start)
        month_pnl = sum(_cashflow(r) for r in rows if _row_dt(r) >= month_start)
        ytd_pnl = sum(_cashflow(r) for r in rows)

        # Try to get live unrealized PnL
        unrealized_total: Optional[float] = None
        try:
            from ib_async import Option
            ib = await get_ib_connection()
            portfolio = ib.portfolio(config.runtime.account.number)
            ib.disconnect()
            unrealized_total = sum(
                item.unrealizedPNL for item in portfolio if item.position != 0
            )
        except Exception:
            pass

        def _sign_emoji(v: float) -> str:
            return "🟢" if v >= 0 else "🔴"

        msg = "💰 <b>Realized Option Premium P&L</b>\n\n"
        msg += f"{_sign_emoji(today_pnl)} Today:       <b>{_money(today_pnl)}</b>\n"
        msg += f"{_sign_emoji(week_pnl)} This Week:   <b>{_money(week_pnl)}</b>\n"
        msg += f"{_sign_emoji(month_pnl)} This Month:  <b>{_money(month_pnl)}</b>\n"
        msg += f"{_sign_emoji(ytd_pnl)} YTD / Since {_revenue_start_datetime().strftime('%Y-%m-%d')}: <b>{_money(ytd_pnl)}</b>\n"
        msg += f"\n📊 Total executions counted: <b>{len(rows)}</b>\n"

        if unrealized_total is not None:
            emoji = _sign_emoji(unrealized_total)
            msg += f"\n{emoji} <b>Unrealized P&L (live):</b> <b>{_money(unrealized_total)}</b>\n"
            msg += f"{'🟢' if ytd_pnl + unrealized_total >= 0 else '🔴'} <b>Total (realized + unrealized):</b> <b>{_money(ytd_pnl + unrealized_total)}</b>"
        else:
            msg += "\n⚠️ Unrealized P&L unavailable (IBKR offline)"

        await status_msg.edit_text(msg, parse_mode="HTML")
    except Exception as e:
        await status_msg.edit_text(f"Error calculating P&L: {html.escape(str(e))}")


async def theta_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config:
        await update.message.reply_text("Error: Config not loaded.")
        return

    status_msg = await update.message.reply_text("Fetching greeks from IBKR (may take ~10s)...")
    ib = None
    try:
        from ib_async import Option, util

        ib = await get_ib_connection()
        portfolio = ib.portfolio(config.runtime.account.number)

        option_items = [
            item for item in portfolio
            if isinstance(item.contract, Option) and item.position != 0
        ]

        if not option_items:
            ib.disconnect()
            await status_msg.edit_text("⏳ No open option positions found.")
            return

        # Request market data for all option contracts
        tickers = {}
        for item in option_items:
            ticker = ib.reqMktData(item.contract, genericTickList="")
            tickers[item.contract.conId] = ticker

        # Wait for model greeks to populate (up to 10s)
        await asyncio.sleep(10)

        rows = []
        total_theta_day = 0.0
        for item in option_items:
            contract = item.contract
            qty = item.position
            ticker = tickers.get(contract.conId)
            theta_per_share: Optional[float] = None
            if ticker and ticker.modelGreeks and not util.isNan(ticker.modelGreeks.theta or float("nan")):
                theta_per_share = ticker.modelGreeks.theta
            multiplier = _option_multiplier(contract)
            # theta_per_share is per-share per day, multiply by 100 (multiplier) × contracts
            if theta_per_share is not None:
                theta_day = theta_per_share * abs(qty) * multiplier
                # For SHORT positions, theta is positive income (theta sign from IBKR is negative for long options)
                total_theta_day += theta_day
            else:
                theta_day = None

            from thetagang.options import option_dte
            dte = option_dte(contract.lastTradeDateOrContractMonth)
            right = "CALL" if contract.right == "C" else "PUT"
            side = "SHORT" if qty < 0 else "LONG"
            rows.append((contract, qty, dte, right, side, theta_day))

        ib.disconnect()

        # Sort: SHORT first (they earn theta), then by DTE
        rows.sort(key=lambda r: (0 if r[4] == "SHORT" else 1, r[2]))

        msg = "⏳ <b>Daily Theta Decay by Position</b>\n\n"
        available_count = 0
        for contract, qty, dte, right, side, theta_day in rows:
            abs_qty = int(abs(qty))
            if theta_day is not None:
                sign = "+" if theta_day >= 0 else ""
                theta_str = f"<b>{sign}{_money(theta_day)}/day</b>"
                available_count += 1
            else:
                theta_str = "<i>n/a (greeks unavailable)</i>"
            side_emoji = "🔴" if side == "SHORT" else "🟢"
            msg += (
                f"{side_emoji} <b>{contract.symbol}</b> {side} {right} "
                f"${contract.strike:g} × {abs_qty} [{dte} DTE] — {theta_str}\n"
            )

        if available_count > 0:
            sign = "+" if total_theta_day >= 0 else ""
            msg += f"\n💎 <b>Portfolio Theta: {sign}{_money(total_theta_day)}/day</b>"
            msg += "\n<i>Positive = net theta collection (short premium earns time decay)</i>"
        else:
            msg += "\n⚠️ Greeks unavailable — market may be closed or delayed data"

        await status_msg.edit_text(msg, parse_mode="HTML")

    except Exception as e:
        if ib and ib.isConnected():
            ib.disconnect()
        await status_msg.edit_text(f"Error fetching theta: {html.escape(str(e))}")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config or not config_path:
        await update.message.reply_text("Error: Config not loaded.")
        return

    try:
        limit = int(context.args[0]) if context.args else 10
        limit = max(1, min(limit, 30))
    except (ValueError, IndexError):
        limit = 10

    try:
        from sqlalchemy import create_engine, text as sa_text

        db_url = config.runtime.database.resolve_url(config_path)
        engine = create_engine(db_url, future=True)

        query = sa_text(
            """
            SELECT
                r.id,
                r.started_at,
                r.hostname,
                r.dry_run,
                COUNT(CASE WHEN e.event_type = 'order_enqueued' THEN 1 END) AS orders_sent,
                MAX(CASE WHEN e.event_type = 'run_end' THEN e.payload END) AS run_end_payload,
                MAX(CASE WHEN e.event_type = 'run_end' THEN e.created_at END) AS ended_at
            FROM runs r
            LEFT JOIN events e ON e.run_id = r.id
            WHERE r.config_path = :config_path
              AND r.dry_run = 0
            GROUP BY r.id
            ORDER BY r.started_at DESC
            LIMIT :limit
            """
        )

        with engine.connect() as conn:
            rows = list(conn.execute(query, {"config_path": config_path, "limit": limit}).mappings())

        if not rows:
            await update.message.reply_html("📋 No run history found in database.")
            return

        msg = f"📋 <b>Trading Engine Run History</b> (last {len(rows)})\n\n"
        for row in rows:
            run_id = row["id"]
            started = str(row["started_at"] or "")[:16]
            hostname = str(row["hostname"] or "?")[:12]
            orders = row["orders_sent"] or 0
            ended_at = row["ended_at"]
            run_end_payload = row["run_end_payload"]

            if ended_at:
                success = True
                try:
                    import json as _json
                    success = _json.loads(str(run_end_payload)).get("success", True)
                except Exception:
                    pass
                status_emoji = "✅" if success else "❌"
                ended_str = str(ended_at)[:16]
                # Compute duration
                try:
                    from datetime import datetime as _dt
                    s = _dt.fromisoformat(str(row["started_at"])[:19])
                    e_ = _dt.fromisoformat(str(ended_at)[:19])
                    secs = int((e_ - s).total_seconds())
                    duration = f"{secs // 60}m{secs % 60}s" if secs >= 60 else f"{secs}s"
                except Exception:
                    duration = "?"
                status_line = f"{status_emoji} {duration} | {orders} orders"
            else:
                status_line = "⏳ no run_end (exchange closed / waiting)"

            msg += (
                f"<code>#{run_id}</code> <code>{started}</code> "
                f"[<code>{hostname}</code>]\n"
                f"  {status_line}\n"
            )

        await update.message.reply_html(msg)
    except Exception as e:
        await update.message.reply_text(f"Error fetching history: {html.escape(str(e))}")


async def events_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config or not config_path:
        await update.message.reply_text("Error: Config not loaded.")
        return

    symbol_filter = context.args[0].upper() if context.args else None

    try:
        import json as _json
        from sqlalchemy import create_engine, text as sa_text

        db_url = config.runtime.database.resolve_url(config_path)
        engine = create_engine(db_url, future=True)

        if symbol_filter:
            query = sa_text(
                """
                SELECT e.event_type, e.symbol, e.payload, e.created_at, r.id AS run_id
                FROM events e JOIN runs r ON r.id = e.run_id
                WHERE r.config_path = :config_path
                  AND r.dry_run = 0
                  AND (e.symbol = :symbol OR e.event_type IN ('run_start', 'run_end'))
                ORDER BY e.created_at DESC LIMIT 30
                """
            )
            params: dict = {"config_path": config_path, "symbol": symbol_filter}
        else:
            query = sa_text(
                """
                SELECT e.event_type, e.symbol, e.payload, e.created_at, r.id AS run_id
                FROM events e JOIN runs r ON r.id = e.run_id
                WHERE r.config_path = :config_path
                  AND r.dry_run = 0
                ORDER BY e.created_at DESC LIMIT 25
                """
            )
            params = {"config_path": config_path}

        with engine.connect() as conn:
            rows = list(conn.execute(query, params).mappings())

        if not rows:
            msg = f"📜 No events found{' for ' + symbol_filter if symbol_filter else ''}."
            await update.message.reply_html(msg)
            return

        title = f"📜 <b>Recent Events</b>"
        if symbol_filter:
            title += f" — <b>{html.escape(symbol_filter)}</b>"
        msg = title + "\n\n"

        for row in rows:
            etype = str(row["event_type"] or "")
            sym = str(row["symbol"] or "")
            created = str(row["created_at"] or "")[:16]
            run_id = row["run_id"]
            payload_raw = row["payload"]

            # Parse payload for readable summary
            try:
                p = _json.loads(str(payload_raw)) if payload_raw else {}
            except Exception:
                p = {}

            if etype == "order_enqueued":
                action = p.get("action", "?")
                qty = p.get("quantity", "?")
                sec = p.get("sec_type", "?")
                price = p.get("limit_price")
                price_str = f"${price:.2f}" if price is not None else "MKT"
                detail = f"{action} {qty} {sec} @ {price_str}"
                emoji = "🛒" if action == "BUY" else "📤"
            elif etype == "run_start":
                detail = "trading engine started"
                emoji = "▶️"
                sym = ""
            elif etype == "run_end":
                ok = p.get("success", True)
                detail = "completed ✓" if ok else "completed with error ✗"
                emoji = "✅" if ok else "❌"
                sym = ""
            else:
                detail = str(payload_raw or "")[:60]
                emoji = "•"

            sym_part = f" <b>{html.escape(sym)}</b>" if sym else ""
            msg += (
                f"{emoji} <code>{created}</code>{sym_part} "
                f"<i>{html.escape(etype)}</i>\n"
                f"   {html.escape(detail)}\n"
            )

        await update.message.reply_html(msg)
    except Exception as e:
        await update.message.reply_text(f"Error fetching events: {html.escape(str(e))}")


async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config:
        await update.message.reply_text("Error: Config not loaded.")
        return

    status_msg = await update.message.reply_text("Fetching live orders from IBKR...")
    try:
        ib = await get_ib_connection()
        open_trades = await ib.reqAllOpenOrdersAsync()
        ib.disconnect()

        if not open_trades:
            await status_msg.edit_text("📌 No open orders at broker.")
            return

        msg = f"📌 <b>Live Open Orders ({len(open_trades)})</b>\n\n"
        for trade in open_trades:
            contract = trade.contract
            order = trade.order
            status = trade.orderStatus

            sym = html.escape(getattr(contract, "symbol", "?"))
            local_sym = html.escape(getattr(contract, "localSymbol", "") or "")
            sec_type = getattr(contract, "secType", "")
            action = getattr(order, "action", "?")
            qty = float(getattr(order, "totalQuantity", 0) or 0)
            lmt = getattr(order, "lmtPrice", None)
            lmt_str = _money(float(lmt)) if lmt and float(lmt) != 0 else "MKT"
            order_type = getattr(order, "orderType", "?")
            tif = getattr(order, "tif", "")
            status_str = getattr(status, "status", "?")
            filled = float(getattr(status, "filled", 0) or 0)
            remaining = float(getattr(status, "remaining", qty) or 0)
            avg_fill = getattr(status, "avgFillPrice", None)
            order_id = getattr(order, "orderId", "?")

            fill_pct = (filled / qty * 100) if qty > 0 else 0
            fill_bar = f"{fill_pct:.0f}% filled" if filled > 0 else "unfilled"

            status_emoji = {
                "Filled": "✅",
                "PartiallyFilled": "🔄",
                "Submitted": "⏳",
                "PreSubmitted": "🔜",
                "PendingSubmit": "🔜",
                "Cancelled": "🚫",
            }.get(status_str, "❓")

            contract_label = f"{sym}" + (f" ({local_sym})" if local_sym and local_sym != sym else "")

            msg += (
                f"{status_emoji} <b>{action} {qty:g} {contract_label}</b> [{sec_type}]\n"
                f"   {order_type} @ {lmt_str} | {tif} | {status_str} | {fill_bar}\n"
            )
            if filled > 0 and avg_fill:
                msg += f"   Avg fill: {_money(float(avg_fill))}\n"
            msg += f"   OrderId: <code>{order_id}</code>\n\n"

        await status_msg.edit_text(msg, parse_mode="HTML")
    except Exception as e:
        await status_msg.edit_text(
            _ibkr_err(e),
            parse_mode="HTML",
        )


async def greeks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Portfolio-level aggregated delta/gamma/theta/vega per symbol + totals."""
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config:
        await update.message.reply_text("Error: Config not loaded.")
        return

    status_msg = await update.message.reply_text("Fetching portfolio greeks (~15s)...")
    ib = None
    try:
        from ib_async import Option, util

        ib = await get_ib_connection()
        portfolio = ib.portfolio(config.runtime.account.number)

        option_items = [i for i in portfolio if isinstance(i.contract, Option) and i.position != 0]
        if not option_items:
            ib.disconnect()
            await status_msg.edit_text("🔢 No open option positions.")
            return

        tickers = {i.contract.conId: ib.reqMktData(i.contract, genericTickList="") for i in option_items}
        await asyncio.sleep(12)

        UNSET = float("nan")
        def _greek(ticker, attr: str) -> Optional[float]:
            g = getattr(ticker, "modelGreeks", None)
            if g is None:
                return None
            v = getattr(g, attr, UNSET)
            return None if (v is None or util.isNan(v)) else v

        by_symbol: dict[str, dict] = {}
        port_delta = port_gamma = port_theta = port_vega = 0.0
        any_greeks = False

        for item in option_items:
            sym = item.contract.symbol
            qty = item.position
            mult = _option_multiplier(item.contract)
            ticker = tickers.get(item.contract.conId)
            right = "CALL" if item.contract.right == "C" else "PUT"
            dte = 0
            try:
                from thetagang.options import option_dte
                dte = option_dte(item.contract.lastTradeDateOrContractMonth)
            except Exception:
                pass

            g = by_symbol.setdefault(sym, {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "lines": []})

            delta = _greek(ticker, "delta")
            gamma = _greek(ticker, "gamma")
            theta = _greek(ticker, "theta")
            vega  = _greek(ticker, "vega")

            if delta is not None:
                any_greeks = True
                pos_delta  = delta * qty * mult
                pos_gamma  = (gamma or 0) * qty * mult
                pos_theta  = (theta or 0) * qty * mult
                pos_vega   = (vega  or 0) * qty * mult
                g["delta"] += pos_delta;  port_delta += pos_delta
                g["gamma"] += pos_gamma;  port_gamma += pos_gamma
                g["theta"] += pos_theta;  port_theta += pos_theta
                g["vega"]  += pos_vega;   port_vega  += pos_vega
                greek_str = (
                    f"Δ{pos_delta:+.2f} Γ{pos_gamma:+.4f} "
                    f"Θ{pos_theta:+.2f} V{pos_vega:+.2f}"
                )
            else:
                greek_str = "<i>n/a</i>"

            side = "SHORT" if qty < 0 else "LONG"
            g["lines"].append(
                f"  {'🔴' if qty<0 else '🟢'} {side} {right} "
                f"${item.contract.strike:g}×{int(abs(qty))} [{dte}d] — {greek_str}"
            )

        ib.disconnect()

        msg = "🔢 <b>Portfolio Greeks</b>\n\n"
        for sym, g in sorted(by_symbol.items()):
            msg += f"<b>[{html.escape(sym)}]</b>  Δ{g['delta']:+.2f} Θ{g['theta']:+.2f}\n"
            msg += "\n".join(g["lines"]) + "\n\n"

        if any_greeks:
            msg += (
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📐 <b>Portfolio Totals</b>\n"
                f"• Delta:  <b>{port_delta:+.2f}</b>  (net directional USD/pt)\n"
                f"• Gamma:  <b>{port_gamma:+.4f}</b>  (delta change/pt²)\n"
                f"• Theta:  <b>{port_theta:+.2f}</b>  (USD/day time decay)\n"
                f"• Vega:   <b>{port_vega:+.2f}</b>  (USD/1% IV move)\n"
            )
            if port_delta > 0:
                msg += "\n<i>⚠️ Net long delta — bullish directional exposure</i>"
            elif port_delta < -0.5:
                msg += "\n<i>⚠️ Net short delta — bearish directional exposure</i>"
            else:
                msg += "\n<i>✅ Near-neutral delta</i>"
        else:
            msg += "⚠️ Greeks unavailable — market may be closed or delayed"

        await status_msg.edit_text(msg, parse_mode="HTML")
    except Exception as e:
        if ib and ib.isConnected():
            ib.disconnect()
        await status_msg.edit_text(f"Error fetching greeks: {html.escape(str(e))}")


async def iv_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/iv <symbol> — Current IV + 52-week IV rank for a symbol."""
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config:
        await update.message.reply_text("Error: Config not loaded.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /iv <symbol>  e.g. /iv TSLA")
        return

    symbol = context.args[0].upper()
    status_msg = await update.message.reply_text(f"Fetching IV data for {symbol} (~15s)...")
    ib = None
    try:
        from ib_async import Stock, util

        ib = await get_ib_connection()

        # Qualify the stock contract
        stk = Stock(symbol, "SMART", "USD")
        contracts = await ib.qualifyContractsAsync(stk)
        if not contracts:
            await status_msg.edit_text(f"Could not find contract for {symbol}.")
            ib.disconnect()
            return
        stk = contracts[0]

        # Fetch 1Y of daily IV history
        bars = await ib.reqHistoricalDataAsync(
            stk, "", "1 Y", "1 day", "OPTION_IMPLIED_VOLATILITY", True
        )

        if not bars:
            await status_msg.edit_text(f"No IV history available for {symbol}.")
            ib.disconnect()
            return

        iv_values = [b.close for b in bars if b.close and b.close > 0]
        if not iv_values:
            await status_msg.edit_text(f"IV data empty for {symbol}.")
            ib.disconnect()
            return

        current_iv = iv_values[-1]
        iv_52w_high = max(iv_values)
        iv_52w_low  = min(iv_values)
        iv_rank = ((current_iv - iv_52w_low) / (iv_52w_high - iv_52w_low) * 100) if (iv_52w_high - iv_52w_low) > 0 else 0.0
        iv_pct   = sum(1 for v in iv_values if v <= current_iv) / len(iv_values) * 100

        # IV regime label
        if iv_rank >= 80:
            regime = "🔥 Extremely High — premium selling ideal"
        elif iv_rank >= 60:
            regime = "🟠 High — good for writing options"
        elif iv_rank >= 40:
            regime = "🟡 Neutral"
        elif iv_rank >= 20:
            regime = "🟢 Low — premium is thin"
        else:
            regime = "🔵 Very Low — avoid selling premium"

        # Also fetch current stock price for context
        ticker = ib.reqMktData(stk, genericTickList="")
        await asyncio.sleep(3)
        stock_price = ticker.marketPrice() if ticker and not util.isNan(ticker.marketPrice()) else None
        ib.disconnect()

        msg = (
            f"📊 <b>IV Analysis — {html.escape(symbol)}</b>\n\n"
            f"• Current IV:    <b>{current_iv * 100:.1f}%</b>\n"
            f"• 52w High:      <b>{iv_52w_high * 100:.1f}%</b>\n"
            f"• 52w Low:       <b>{iv_52w_low * 100:.1f}%</b>\n\n"
            f"• IV Rank:       <b>{iv_rank:.0f} / 100</b>\n"
            f"• IV Percentile: <b>{iv_pct:.0f}%</b>\n\n"
            f"Regime: {regime}\n"
        )
        if stock_price:
            msg += f"\n• Last price: <b>{_money(stock_price)}</b>"

        await status_msg.edit_text(msg, parse_mode="HTML")
    except Exception as e:
        if ib and ib.isConnected():
            ib.disconnect()
        await status_msg.edit_text(f"Error fetching IV for {symbol}: {html.escape(str(e))}")


async def whatif_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/whatif <symbol> — Simulate closing all positions for a symbol, show margin impact."""
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config:
        await update.message.reply_text("Error: Config not loaded.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /whatif <symbol>  e.g. /whatif TSLA")
        return

    symbol = context.args[0].upper()
    status_msg = await update.message.reply_text(f"Simulating close of {symbol} positions...")
    ib = None
    try:
        from ib_async import MarketOrder, util

        ib = await get_ib_connection()
        portfolio = ib.portfolio(config.runtime.account.number)

        targets = [
            i for i in portfolio
            if i.contract.symbol.upper() == symbol and i.position != 0
        ]
        if not targets:
            await status_msg.edit_text(f"No open positions found for {symbol}.")
            ib.disconnect()
            return

        results = []
        total_maint_change = 0.0
        total_init_change  = 0.0
        total_equity_change = 0.0
        total_commission   = 0.0

        for item in targets:
            qty    = item.position
            action = "BUY" if qty < 0 else "SELL"
            order  = MarketOrder(action, abs(qty))
            order.whatIf = True
            state  = await ib.whatIfOrderAsync(item.contract, order)

            def _parse(s: str) -> float:
                try:
                    return float(s)
                except (TypeError, ValueError):
                    return 0.0

            maint_chg  = _parse(state.maintMarginChange)
            init_chg   = _parse(state.initMarginChange)
            equity_chg = _parse(state.equityWithLoanChange)
            commission = state.commission if state.commission and not util.isNan(state.commission) else 0.0

            total_maint_change  += maint_chg
            total_init_change   += init_chg
            total_equity_change += equity_chg
            total_commission    += commission

            contract = item.contract
            sec = contract.secType
            label = contract.localSymbol or contract.symbol
            results.append({
                "label":      label,
                "sec":        sec,
                "action":     action,
                "qty":        abs(qty),
                "maint_chg":  maint_chg,
                "commission": commission,
                "mkt_value":  item.marketValue,
            })

        ib.disconnect()

        freed = total_maint_change < 0
        emoji = "🟢" if freed else "🔴"

        msg = f"🔮 <b>What-If: Close All {html.escape(symbol)}</b>\n\n"
        for r in results:
            chg_str = _money(abs(r["maint_chg"]))
            direction = "frees" if r["maint_chg"] < 0 else "uses"
            msg += (
                f"• {r['action']} {r['qty']:g} <b>{html.escape(r['label'])}</b> [{r['sec']}]\n"
                f"  Margin {direction} <b>{chg_str}</b> | commission ~{_money(r['commission'])}\n"
            )

        msg += (
            f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} <b>Net Impact</b>\n"
            f"• Maint. Margin:  <b>{_money(total_maint_change)}</b> "
            f"({'freed' if freed else 'added'})\n"
            f"• Init. Margin:   <b>{_money(total_init_change)}</b>\n"
            f"• Equity change:  <b>{_money(total_equity_change)}</b>\n"
            f"• Est. commission: <b>~{_money(total_commission)}</b>\n"
        )
        msg += "\n<i>⚠️ What-if simulation only — no order placed.</i>"

        await status_msg.edit_text(msg, parse_mode="HTML")
    except Exception as e:
        if ib and ib.isConnected():
            ib.disconnect()
        await status_msg.edit_text(f"Error in what-if simulation: {html.escape(str(e))}")


async def attribution_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """P&L attribution: put premium / call premium / rolls / stock / SGOV."""
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config or not config_path:
        await update.message.reply_text("Error: Config not loaded.")
        return

    status_msg = await update.message.reply_text("Building P&L attribution...")
    try:
        import json as _json
        from sqlalchemy import create_engine, text as sa_text

        db_url = config.runtime.database.resolve_url(config_path)
        engine = create_engine(db_url, future=True)
        ytd_start = _revenue_start_datetime()

        query = sa_text(
            """
            SELECT
                e.side, e.shares, e.price, e.symbol, e.execution_time,
                o.sec_type, o.action, o.intent_id,
                oi.payload_json
            FROM executions e
            JOIN orders o ON o.order_id = e.order_id
            LEFT JOIN order_intents oi ON oi.id = o.intent_id
            WHERE e.execution_time >= :ytd_start
            ORDER BY e.execution_time ASC
            """
        )

        with engine.connect() as conn:
            rows = list(conn.execute(query, {"ytd_start": ytd_start}).mappings())

        # Buckets: put_premium, call_premium, rolls, stock, sgov, other
        buckets: dict[str, float] = {
            "put":   0.0,
            "call":  0.0,
            "roll":  0.0,
            "stock": 0.0,
            "sgov":  0.0,
            "other": 0.0,
        }
        by_symbol: dict[str, float] = {}

        for row in rows:
            side     = str(row.get("side") or "")
            shares   = float(row.get("shares") or 0)
            price    = float(row.get("price") or 0)
            symbol   = str(row.get("symbol") or "?")
            sec_type = str(row.get("sec_type") or "")
            sign     = 1.0 if side == "SLD" else -1.0

            if sec_type == "OPT":
                cf = sign * shares * price * 100
                # Extract right from order_intent payload
                right = None
                payload = row.get("payload_json")
                if payload:
                    try:
                        p = _json.loads(str(payload))
                        right = p.get("contract", {}).get("right", "")
                    except Exception:
                        pass
                if right == "P":
                    buckets["put"] += cf
                elif right == "C":
                    buckets["call"] += cf
                else:
                    buckets["other"] += cf
                by_symbol[symbol] = by_symbol.get(symbol, 0.0) + cf

            elif sec_type == "BAG":
                # Combo/roll orders: price is net debit/credit per unit
                cf = sign * shares * price * 100
                buckets["roll"] += cf
                by_symbol[symbol] = by_symbol.get(symbol, 0.0) + cf

            elif sec_type == "STK":
                cf = sign * shares * price
                if symbol.upper() == "SGOV":
                    buckets["sgov"] += cf
                else:
                    buckets["stock"] += cf
                by_symbol[symbol] = by_symbol.get(symbol, 0.0) + cf

        # Try to get live unrealized P&L by type
        unrealized_opts: Optional[float] = None
        unrealized_stk: Optional[float] = None
        try:
            from ib_async import Option, Stock
            ib = await get_ib_connection()
            portfolio = ib.portfolio(config.runtime.account.number)
            ib.disconnect()
            unrealized_opts = sum(i.unrealizedPNL for i in portfolio if isinstance(i.contract, Option) and i.position != 0)
            unrealized_stk  = sum(i.unrealizedPNL for i in portfolio if isinstance(i.contract, Stock)  and i.position != 0)
        except Exception:
            pass

        total_realized = sum(buckets.values())

        def _row(label: str, v: float) -> str:
            emoji = "🟢" if v > 0 else ("🔴" if v < 0 else "⚪")
            return f"{emoji} {label:<20} <b>{_money(v)}</b>\n"

        msg = (
            f"🧮 <b>P&L Attribution</b> "
            f"<i>(since {ytd_start.strftime('%Y-%m-%d')})</i>\n\n"
            "<b>Realized (Option Premium)</b>\n"
        )
        msg += _row("Put premium",   buckets["put"])
        msg += _row("Call premium",  buckets["call"])
        msg += _row("Roll credits",  buckets["roll"])
        if buckets["other"]:
            msg += _row("Other OPT",  buckets["other"])

        msg += "\n<b>Realized (Equity)</b>\n"
        msg += _row("Stock (STK)",   buckets["stock"])
        msg += _row("SGOV / Cash",   buckets["sgov"])

        msg += f"\n{'🟢' if total_realized>=0 else '🔴'} <b>Total Realized:  {_money(total_realized)}</b>\n"

        if unrealized_opts is not None or unrealized_stk is not None:
            msg += "\n<b>Unrealized (Live)</b>\n"
            if unrealized_opts is not None:
                msg += _row("Options",  unrealized_opts)
            if unrealized_stk is not None:
                msg += _row("Stocks",   unrealized_stk)
            total_unreal = (unrealized_opts or 0) + (unrealized_stk or 0)
            grand = total_realized + total_unreal
            msg += f"\n{'🟢' if grand>=0 else '🔴'} <b>Grand Total:     {_money(grand)}</b>\n"
        else:
            msg += "\n⚠️ Unrealized P&L unavailable (IBKR offline)\n"

        # Per-symbol breakdown
        if by_symbol:
            msg += "\n<b>By Symbol (realized)</b>\n"
            for sym, v in sorted(by_symbol.items(), key=lambda x: -abs(x[1])):
                emoji = "🟢" if v > 0 else "🔴"
                msg += f"{emoji} <b>{html.escape(sym)}</b>: {_money(v)}\n"

        await status_msg.edit_text(msg, parse_mode="HTML")
    except Exception as e:
        await status_msg.edit_text(f"Error building attribution: {html.escape(str(e))}")


async def leaps_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/leaps <symbol> — Suggest best LEAPS call to buy for PMCC (delta 0.70-0.80)."""
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config:
        await update.message.reply_text("Error: Config not loaded.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /leaps <symbol>  e.g. /leaps NVDA")
        return

    symbol = context.args[0].upper()
    status_msg = await update.message.reply_text(f"Scanning LEAPS calls for {symbol} (~15s)...")
    ib = None
    try:
        from ib_async import LimitOrder, Option, Stock, util

        ib = await get_ib_connection()
        ib.reqMarketDataType(3)

        stk = Stock(symbol, "SMART", "USD")
        contracts = await ib.qualifyContractsAsync(stk)
        if not contracts:
            await status_msg.edit_text(f"Could not find stock contract for {symbol}.")
            ib.disconnect()
            return
        stk = contracts[0]

        stk_ticker = ib.reqMktData(stk, genericTickList="")
        await asyncio.sleep(4)

        def _valid_px(p) -> float | None:
            return p if p and not util.isNan(p) and p > 0 else None

        mid = (
            (stk_ticker.bid + stk_ticker.ask) / 2
            if stk_ticker.bid and stk_ticker.ask
            and _valid_px(stk_ticker.bid) and _valid_px(stk_ticker.ask)
            else None
        )
        stock_price = (
            _valid_px(stk_ticker.last)
            or _valid_px(stk_ticker.close)
            or _valid_px(mid)
            or 0.0
        )

        chains = await ib.reqSecDefOptParamsAsync(symbol, "", "STK", stk.conId)
        chain = next((c for c in chains if c.exchange == "SMART"), chains[0] if chains else None)
        if not chain:
            await status_msg.edit_text(f"No option chain found for {symbol}.")
            ib.disconnect()
            return

        today = date.today()
        # Find expiries 10-20 months out (LEAPS range)
        leaps_expiries = sorted(
            e for e in chain.expirations
            if 300 <= (datetime.strptime(e, "%Y%m%d").date() - today).days <= 600
        )
        if not leaps_expiries:
            leaps_expiries = sorted(
                e for e in chain.expirations
                if (datetime.strptime(e, "%Y%m%d").date() - today).days >= 200
            )[:3]

        if not leaps_expiries:
            await status_msg.edit_text(f"No LEAPS expiries found for {symbol}.")
            ib.disconnect()
            return

        target_expiry = leaps_expiries[0]
        # Target strikes: 10-30% below current price for delta 0.70-0.85
        all_strikes = sorted(chain.strikes)
        low = stock_price * 0.70
        high = stock_price * 0.95
        candidate_strikes = [s for s in all_strikes if low <= s <= high]
        if not candidate_strikes:
            candidate_strikes = [s for s in all_strikes if s <= stock_price][-6:]

        opt_contracts = [Option(symbol, target_expiry, s, "C", "SMART") for s in candidate_strikes]
        qualified = [c for c in await ib.qualifyContractsAsync(*opt_contracts) if c is not None]
        tickers = [ib.reqMktData(c, genericTickList="106") for c in qualified]
        await asyncio.sleep(10)

        expiry_fmt = f"{target_expiry[:4]}-{target_expiry[4:6]}-{target_expiry[6:]}"
        candidates = []
        for c, t in zip(qualified, tickers):
            if not t.modelGreeks:
                continue
            delta = t.modelGreeks.delta
            if delta is None or util.isNan(delta):
                continue
            bid = t.bid if t.bid and t.bid > 0 else 0.0
            ask = t.ask if t.ask and t.ask > 0 else 0.0
            mid = (bid + ask) / 2 if (bid + ask) > 0 else (t.last or 0.0)
            if mid <= 0:
                continue
            candidates.append((c.strike, delta, bid, ask, mid, c.conId))

        ib.disconnect()

        if not candidates:
            await status_msg.edit_text(
                f"⚠️ No greek data available for {symbol} LEAPS — market may be closed.\n"
                f"Target: {symbol} {expiry_fmt} CALL, strike ~{stock_price * 0.85:.0f}–{stock_price * 0.90:.0f} (delta 0.70–0.80)"
            )
            return

        # Sort by delta closest to 0.75
        candidates.sort(key=lambda x: abs(x[1] - 0.75))
        best = candidates[0]
        top3 = candidates[:3]
        top3.sort(key=lambda x: x[0])

        strike, delta, bid, ask, mid, con_id = best
        total_cost = mid * 100

        msg = (
            f"📊 <b>LEAPS Call Suggestions — {html.escape(symbol)}</b>\n"
            f"Stock: <b>{_money(stock_price)}</b>  |  Expiry: <b>{expiry_fmt}</b>\n\n"
            "<b>Top Candidates (sorted by strike):</b>\n"
        )
        for s, d, b, a, m, cid in top3:
            best_marker = " ⭐" if s == strike else ""
            msg += (
                f"• Strike <b>${s:g}</b>  Δ<b>{d:.2f}</b>  "
                f"bid {_money(b)} / ask {_money(a)}  "
                f"mid <b>{_money(m)}</b> (~<b>{_money(m*100)}</b>/contract){best_marker}\n"
                f"  conId: <code>{cid}</code>\n"
            )

        msg += (
            f"\n⭐ <b>Recommended:</b> {html.escape(symbol)} {expiry_fmt} C${strike:g}\n"
            f"• Delta: <b>{delta:.2f}</b>  |  Estimated cost: <b>~{_money(total_cost)}</b>\n"
            f"• conId: <code>{con_id}</code>\n\n"
            f"To buy via bot: <code>/buy_leaps {html.escape(symbol)} {target_expiry} {strike:g}</code>"
        )
        await status_msg.edit_text(msg, parse_mode="HTML")

    except Exception as e:
        if ib and ib.isConnected():
            ib.disconnect()
        if isinstance(e, IBKROfflineError):
            await status_msg.edit_text(_ibkr_err(e), parse_mode="HTML")
        else:
            await status_msg.edit_text(f"Error scanning LEAPS for {symbol}: {html.escape(str(e))}")


async def buy_leaps_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/buy_leaps <symbol> <expiry YYYYMMDD> <strike> — Place a limit buy for a LEAPS call."""
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config:
        await update.message.reply_text("Error: Config not loaded.")
        return
    if len(context.args) != 3:
        await update.message.reply_text(
            "Usage: /buy_leaps <symbol> <expiry YYYYMMDD> <strike>\n"
            "Example: /buy_leaps NVDA 20270115 180"
        )
        return

    symbol = context.args[0].upper()
    expiry = context.args[1]
    try:
        strike = float(context.args[2])
    except ValueError:
        await update.message.reply_text("Strike must be a number.")
        return

    status_msg = await update.message.reply_text(
        f"Preparing limit buy for {symbol} {expiry} C${strike:g}..."
    )
    ib = None
    try:
        from ib_async import LimitOrder, Option, util

        ib = await get_ib_connection()
        ib.reqMarketDataType(3)

        opt = Option(symbol, expiry, strike, "C", "SMART")
        qualified = await ib.qualifyContractsAsync(opt)
        if not qualified:
            await status_msg.edit_text(f"Could not qualify contract: {symbol} {expiry} C${strike:g}")
            ib.disconnect()
            return
        contract = qualified[0]

        ticker = ib.reqMktData(contract, genericTickList="")
        await asyncio.sleep(5)

        bid = ticker.bid if ticker.bid and ticker.bid > 0 else 0.0
        ask = ticker.ask if ticker.ask and ticker.ask > 0 else 0.0
        last = ticker.last if ticker.last and ticker.last > 0 else 0.0
        close = ticker.close if ticker.close and ticker.close > 0 else 0.0

        if bid > 0 and ask > 0:
            limit_price = round((bid + ask) / 2, 2)
        elif last > 0:
            limit_price = round(last * 1.01, 2)
        elif close > 0:
            limit_price = round(close * 1.01, 2)
        else:
            await status_msg.edit_text(
                f"⚠️ Could not determine price for {symbol} {expiry} C${strike:g} (market closed).\n"
                f"Retry during market hours, or place manually in TWS."
            )
            ib.disconnect()
            return

        order = LimitOrder("BUY", 1, limit_price)
        order.tif = "DAY"
        trade = ib.placeOrder(contract, order)
        await asyncio.sleep(2)

        expiry_fmt = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
        order_id = trade.order.orderId
        status_text = trade.orderStatus.status

        ib.disconnect()

        msg = (
            f"📥 <b>LEAPS Buy Order Placed</b>\n\n"
            f"• Contract: <b>{html.escape(symbol)} {expiry_fmt} CALL ${strike:g}</b>\n"
            f"• Action: BUY 1 contract\n"
            f"• Limit price: <b>{_money(limit_price)}</b> (~{_money(limit_price * 100)}/contract)\n"
            f"• TIF: DAY\n"
            f"• Order ID: <code>{order_id}</code>\n"
            f"• Status: <b>{html.escape(status_text)}</b>\n\n"
            f"Use /orders to monitor. After fill, bot will recognize LEAPS on next run."
        )
        await status_msg.edit_text(msg, parse_mode="HTML")

    except Exception as e:
        if ib and ib.isConnected():
            ib.disconnect()
        await status_msg.edit_text(f"Error placing LEAPS order: {html.escape(str(e))}")


async def cancel_order_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel_order <orderId> — Cancel a working open order."""
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /cancel_order <orderId>\nExample: /cancel_order 5")
        return
    try:
        order_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("orderId must be an integer.")
        return

    status_msg = await update.message.reply_text(f"Cancelling order {order_id}...")
    try:
        ib = await get_ib_connection()
        all_trades = await ib.reqAllOpenOrdersAsync()
        trade = next((t for t in all_trades if t.order.orderId == order_id), None)
        if not trade:
            await status_msg.edit_text(f"❌ No open order found with id <code>{order_id}</code>.", parse_mode="HTML")
            ib.disconnect()
            return
        contract = trade.contract
        ib.cancelOrder(trade.order)
        await asyncio.sleep(2)
        ib.disconnect()
        await status_msg.edit_text(
            f"✅ <b>Cancel sent</b>\n"
            f"• Order: <code>{order_id}</code> — {trade.order.action} {trade.order.totalQuantity}"
            f" {contract.symbol} {getattr(contract, 'localSymbol', '')} @ {trade.order.lmtPrice}\n"
            f"• Use /orders to confirm cancellation.",
            parse_mode="HTML",
        )
    except Exception as e:
        await status_msg.edit_text(_ibkr_err(e), parse_mode="HTML")


async def modify_order_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/modify_order <orderId> <newPrice> — Change limit price of a working order."""
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /modify_order <orderId> <newPrice>\nExample: /modify_order 5 54.10"
        )
        return
    try:
        order_id = int(context.args[0])
        new_price = round(float(context.args[1]), 2)
    except ValueError:
        await update.message.reply_text("orderId must be integer, newPrice must be a number.")
        return

    status_msg = await update.message.reply_text(f"Modifying order {order_id} → ${new_price:.2f}...")
    try:
        ib = await get_ib_connection()
        all_trades = await ib.reqAllOpenOrdersAsync()
        trade = next((t for t in all_trades if t.order.orderId == order_id), None)
        if not trade:
            await status_msg.edit_text(f"❌ No open order found with id <code>{order_id}</code>.", parse_mode="HTML")
            ib.disconnect()
            return
        old_price = trade.order.lmtPrice
        trade.order.lmtPrice = new_price
        ib.placeOrder(trade.contract, trade.order)
        await asyncio.sleep(2)
        ib.disconnect()
        contract = trade.contract
        await status_msg.edit_text(
            f"✅ <b>Order modified</b>\n"
            f"• Order: <code>{order_id}</code> — {trade.order.action} {trade.order.totalQuantity}"
            f" {contract.symbol} {getattr(contract, 'localSymbol', '')}\n"
            f"• Price: <b>${old_price:.2f} → ${new_price:.2f}</b>\n"
            f"• Use /orders to confirm.",
            parse_mode="HTML",
        )
    except Exception as e:
        await status_msg.edit_text(_ibkr_err(e), parse_mode="HTML")


async def register_bot_commands(application: Application) -> None:
    """Publish Telegram slash-command menu for clients that show bot commands."""
    await application.bot.set_my_commands([
        BotCommand("0start", "Quick overview — essential status commands only"),
        BotCommand("start", "Show ThetaGang command help"),
        BotCommand("status", "Account summary"),
        BotCommand("positions", "Open positions list"),
        BotCommand("trades", "Executions from the last 3 days"),
        BotCommand("revenue", "Option premium from configured start + next 3M capture"),
        BotCommand("strategy", "Strategy weights and pause status"),
        BotCommand("settings", "Margin, delta, cash/SGOV, and hedge settings"),
        BotCommand("pause", "Pause trading: /pause <symbol|all>"),
        BotCommand("resume", "Resume trading: /resume <symbol|all>"),
        BotCommand("set_weight", "Draft target weight: /set_weight <symbol> <percent>"),
        BotCommand("set_no_trading", "Draft static block: /set_no_trading <symbol> <true|false>"),
        BotCommand("preview_config", "Show pending strategy config diff"),
        BotCommand("apply_config", "Apply validated pending strategy config"),
        BotCommand("discard_config", "Discard pending strategy config draft"),
        BotCommand("reload_strategy", "Reload current TOML into Telegram daemon"),
        BotCommand("close", "Close position: /close <conId|symbol>"),
        BotCommand("cancel_order", "Cancel open order: /cancel_order <orderId>"),
        BotCommand("modify_order", "Change limit price: /modify_order <orderId> <newPrice>"),
        BotCommand("expirations", "Upcoming option expirations (next 60 days)"),
        BotCommand("pnl", "Realized option premium: today / week / month / YTD"),
        BotCommand("theta", "Daily theta decay per position"),
        BotCommand("history", "Last N trading engine runs: /history [N]"),
        BotCommand("events", "Recent decision events: /events [symbol]"),
        BotCommand("orders", "Live open orders at broker (detailed)"),
        BotCommand("greeks", "Portfolio greeks: delta / gamma / theta / vega"),
        BotCommand("iv", "IV rank + 52w IV history: /iv <symbol>"),
        BotCommand("attribution", "P&L breakdown by put/call/roll/stock"),
        BotCommand("whatif", "Simulate close impact on margin: /whatif <symbol>"),
        BotCommand("leaps", "Suggest best LEAPS call for PMCC: /leaps <symbol>"),
        BotCommand("buy_leaps", "Place LEAPS call buy order: /buy_leaps <symbol> <YYYYMMDD> <strike>"),
    ])
    logger.info("Registered Telegram bot command menu")


def start_bot(cfg_path: str) -> None:
    global config, config_path
    
    # Load config file
    raw_config = open(cfg_path, "r", encoding="utf-8").read()
    config_doc = tomlkit.parse(raw_config).unwrap()
    config = Config(**config_doc)
    config_path = cfg_path
    
    if not config.telegram.enabled:
        print("Telegram Bot is disabled in configuration.")
        return
        
    token = config.telegram.bot_token
    if not token:
        print("Telegram Bot token is not configured.")
        return
        
    application = Application.builder().token(token).post_init(start_background_tasks).build()
    
    # Register command handlers
    application.add_handler(CommandHandler("0start", zero_start_command))
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("positions", positions_command))
    application.add_handler(CommandHandler("trades", trades_command))
    application.add_handler(CommandHandler("revenue", revenue_command))
    application.add_handler(CommandHandler("strategy", strategy_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("set_weight", set_weight_command))
    application.add_handler(CommandHandler("set_no_trading", set_no_trading_command))
    application.add_handler(CommandHandler("preview_config", preview_config_command))
    application.add_handler(CommandHandler("apply_config", apply_config_command))
    application.add_handler(CommandHandler("discard_config", discard_config_command))
    application.add_handler(CommandHandler("reload_strategy", reload_strategy_command))
    application.add_handler(CommandHandler("close", close_command))
    application.add_handler(CommandHandler("expirations", expirations_command))
    application.add_handler(CommandHandler("pnl", pnl_command))
    application.add_handler(CommandHandler("theta", theta_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("events", events_command))
    application.add_handler(CommandHandler("orders", orders_command))
    application.add_handler(CommandHandler("greeks", greeks_command))
    application.add_handler(CommandHandler("iv", iv_command))
    application.add_handler(CommandHandler("attribution", attribution_command))
    application.add_handler(CommandHandler("whatif", whatif_command))
    application.add_handler(CommandHandler("leaps", leaps_command))
    application.add_handler(CommandHandler("buy_leaps", buy_leaps_command))
    application.add_handler(CommandHandler("cancel_order", cancel_order_command))
    application.add_handler(CommandHandler("modify_order", modify_order_command))
    
    print(f"Starting ThetaGang Telegram Bot for account {config.runtime.account.number}...")
    application.run_polling()
