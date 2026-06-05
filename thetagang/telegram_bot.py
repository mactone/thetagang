import asyncio
import logging
import json
from pathlib import Path
from typing import Optional

import tomlkit
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from thetagang.config import Config

logger = logging.getLogger(__name__)

# Global config variables
config: Optional[Config] = None
config_path: Optional[str] = None


def is_authorized(chat_id: int) -> bool:
    if not config or not config.telegram.chat_id:
        return False
    return str(chat_id) == str(config.telegram.chat_id)


async def get_ib_connection():
    if not config:
        raise RuntimeError("Config is not loaded")
    from ib_async import IB
    ib = IB()
    await ib.connectAsync(
        config.runtime.watchdog.host,
        config.runtime.watchdog.port,
        clientId=99,
        timeout=10,
    )
    return ib


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    msg = (
        "🤖 <b>ThetaGang Telegram Bot</b>\n\n"
        "/status — Account summary\n"
        "/positions — Open positions list (Stocks & Options)\n"
        "/trades — List the last 30 trade executions\n"
        "/strategy — View strategy weights and status\n"
        "/pause &lt;symbol|all&gt; — Pause automatic trading for a symbol or globally\n"
        "/resume &lt;symbol|all&gt; — Resume automatic trading\n"
        "/close &lt;conId|symbol&gt; — Manually close an open position"
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
        
        from thetagang.util import account_summary_to_dict
        acct_dict = account_summary_to_dict(account_summary)
        
        net_liq = float(acct_dict.get("NetLiquidation", {}).value or 0)
        excess_liq = float(acct_dict.get("ExcessLiquidity", {}).value or 0)
        maint_margin = float(acct_dict.get("FullMaintMarginReq", {}).value or 0)
        cash = float(acct_dict.get("TotalCashValue", {}).value or 0)
        cushion = float(acct_dict.get("Cushion", {}).value or 0)
        buying_power = float(acct_dict.get("BuyingPower", {}).value or 0)
        accrued = float(acct_dict.get("AccruedCash", {}).value or 0)
        
        await ib.disconnectAsync()
        
        message = (
            f"📊 <b>Account Overview</b>\n\n"
            f"💰 <b>Balances:</b>\n"
            f"• Net Liquidation (NAV): <b>${net_liq:,.2f} USD</b>\n"
            f"• Real Cash: <b>${cash:,.2f} USD</b>\n"
            f"• Buying Power: <b>${buying_power:,.2f} USD</b>\n"
            f"• Excess Liquidity: <b>${excess_liq:,.2f} USD</b>\n"
            f"• Maintenance Margin: <b>${maint_margin:,.2f} USD</b>\n"
            f"• Accrued Cash: <b>${accrued:,.2f} USD</b>\n"
            f"• Cushion: <b>{cushion * 100:.1f}%</b>"
        )
        await status_msg.edit_text(message, parse_mode="HTML")
    except Exception as e:
        await status_msg.edit_text(f"Error fetching status: {e}")


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
        
        stocks_info = []
        options_info = []
        
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
            
            if isinstance(contract, Stock):
                stocks_info.append(
                    f"• <b>{contract.symbol}</b>: {qty:.1f} shares @ ${avg_cost:.2f} "
                    f"(Value: ${mkt_value:,.2f}, PnL: ${unrealized_pnl:,.2f} [{pnl_pct:+.1f}%])"
                )
            elif isinstance(contract, Option):
                from thetagang.options import option_dte
                dte = option_dte(contract.lastTradeDateOrContractMonth)
                
                desc = f"{contract.symbol} {contract.lastTradeDateOrContractMonth} {contract.strike} {contract.right}"
                options_info.append(
                    f"• <b>{desc}</b> (conId: <code>{contract.conId}</code>): {qty:.1f} contract(s)\n"
                    f"  Avg Cost: ${avg_cost/100:.2f} | Mkt Price: ${mkt_price:.2f} | DTE: {dte}\n"
                    f"  Value: ${mkt_value:,.2f} | PnL: ${unrealized_pnl:,.2f} [{pnl_pct:+.1f}%]"
                )
        
        await ib.disconnectAsync()
        
        msg = "📦 <b>Open Positions</b>\n\n"
        msg += "<b>[Stocks/ETFs]</b>\n"
        if stocks_info:
            msg += "\n".join(stocks_info)
        else:
            msg += "• No stock positions."
            
        msg += "\n\n<b>[Options]</b>\n"
        if options_info:
            msg += "\n".join(options_info)
        else:
            msg += "• No option positions."
            
        await status_msg.edit_text(msg, parse_mode="HTML")
    except Exception as e:
        await status_msg.edit_text(f"Error fetching positions: {e}")


async def trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_authorized(update.effective_chat.id):
        return
    if not config or not config_path:
        await update.message.reply_text("Error: Config not loaded.")
        return

    status_msg = await update.message.reply_text("Fetching recent trades from database...")
    try:
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import sessionmaker
        from thetagang.db import ExecutionRecord
        
        db_url = config.runtime.database.resolve_url(config_path)
        engine = create_engine(db_url, future=True)
        Session = sessionmaker(bind=engine, future=True)
        
        with Session() as session:
            stmt = (
                select(ExecutionRecord)
                .order_by(ExecutionRecord.execution_time.desc())
                .limit(30)
            )
            executions = session.execute(stmt).scalars().all()
            
        if not executions:
            await status_msg.edit_text("No trade executions found in the database.")
            return
            
        msg = "🔄 <b>Recent Trades (Last 30 Executions)</b>\n\n"
        for ex in executions:
            time_str = ex.execution_time.strftime("%m-%d %H:%M") if ex.execution_time else "-"
            side_str = "🟢 BOT" if ex.side == "BOT" else "🔴 SLD"
            msg += f"• <code>{time_str}</code> | {side_str} <b>{abs(ex.shares):.1f} {ex.symbol}</b> @ ${ex.price:.2f} (Ref: {ex.order_ref or '-'})\n"
            
        await status_msg.edit_text(msg, parse_mode="HTML")
    except Exception as e:
        await status_msg.edit_text(f"Error querying trades: {e}")


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
            await ib.disconnectAsync()
            return
            
        contract = target_item.contract
        qty = target_item.position
        
        action = "SELL" if qty > 0 else "BUY"
        close_qty = abs(qty)
        
        order = MarketOrder(action, close_qty)
        trade = ib.placeOrder(contract, order)
        
        # Wait up to 5 seconds for order fill
        for _ in range(5):
            await asyncio.sleep(1)
            if trade.isDone():
                break
                
        status = trade.orderStatus.status
        avg_price = trade.orderStatus.avgFillPrice
        await ib.disconnectAsync()
        
        if trade.isDone():
            await status_msg.edit_text(
                f"✅ <b>Successfully closed position:</b>\n"
                f"• Contract: {contract.symbol} {contract.localSymbol}\n"
                f"• Order: {action} {close_qty} (Market Order)\n"
                f"• Status: <b>Filled</b> @ average price of ${avg_price:.2f}",
                parse_mode="HTML"
            )
        else:
            await status_msg.edit_text(
                f"⚠️ <b>Close order placed but not yet filled:</b>\n"
                f"• Contract: {contract.symbol} {contract.localSymbol}\n"
                f"• Order: {action} {close_qty}\n"
                f"• Current Status: <b>{status}</b>",
                parse_mode="HTML"
            )
    except Exception as e:
        await status_msg.edit_text(f"Error closing position: {e}")


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
        
    application = Application.builder().token(token).build()
    
    # Register command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("positions", positions_command))
    application.add_handler(CommandHandler("trades", trades_command))
    application.add_handler(CommandHandler("strategy", strategy_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("close", close_command))
    
    print(f"Starting ThetaGang Telegram Bot for account {config.runtime.account.number}...")
    application.run_polling()
