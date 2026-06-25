from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

import thetagang.telegram_bot as bot
from thetagang.db import DataStore


class _DatabaseConfig:
    enabled = True

    def __init__(self, db_url: str) -> None:
        self.db_url = db_url

    def resolve_url(self, _config_path: str) -> str:
        return self.db_url


def test_load_submitted_unfilled_orders_uses_executions_for_remaining(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "state.db"
    config_path = str(tmp_path / "thetagang.toml")
    data_store = DataStore(
        f"sqlite:///{db_path}",
        config_path,
        dry_run=False,
        config_text="test",
    )

    contract = SimpleNamespace(
        symbol="SGOV",
        secType="STK",
        conId=424099317,
        exchange="SMART",
        currency="USD",
    )
    order = SimpleNamespace(
        action="SELL",
        totalQuantity=90,
        lmtPrice=100.45,
        orderType="LMT",
        orderRef="",
        tif="DAY",
        orderId=545,
    )
    data_store.record_order(contract, order)
    data_store.record_order_status(
        SimpleNamespace(
            order=order,
            orderStatus=SimpleNamespace(
                status="Submitted",
                filled=0,
                remaining=90,
                avgFillPrice=0,
                lastFillPrice=0,
            ),
        )
    )
    data_store.record_executions(
        [
            SimpleNamespace(
                execution=SimpleNamespace(
                    execId="sgov-1",
                    orderId=545,
                    orderRef="",
                    side="SLD",
                    shares=80,
                    price=100.46,
                    exchange="BYX",
                    time="20260608 19:50:10",
                ),
                contract=contract,
                time="20260608 19:50:10",
            )
        ]
    )

    monkeypatch.setattr(bot, "config_path", config_path)
    monkeypatch.setattr(
        bot,
        "config",
        SimpleNamespace(
            runtime=SimpleNamespace(database=_DatabaseConfig(f"sqlite:///{db_path}"))
        ),
    )

    rows = bot._load_submitted_unfilled_orders(limit=5)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "SGOV"
    assert rows[0]["filled"] == 80
    assert rows[0]["remaining"] == 10


def test_format_submitted_order_contains_remaining_and_order_id() -> None:
    msg = bot._format_submitted_order(
        {
            "symbol": "PLTR",
            "action": "SELL",
            "quantity": 1,
            "limit_price": 3.06,
            "status": "Submitted",
            "filled": 0,
            "remaining": 1,
            "order_id": 544,
            "created_at": "2026-06-08 16:20:07",
        }
    )

    assert "PLTR" in msg
    assert "remaining 1" in msg
    assert "id 544" in msg


def test_format_db_status_fallback_uses_snapshot_and_orders(monkeypatch) -> None:
    monkeypatch.setattr(
        bot,
        "_load_latest_account_status_snapshot",
        lambda: {
            "run_id": 40,
            "started_at": "2026-06-08 16:19:51",
            "net_liq": 109917.19,
            "cash": 758.98,
            "buying_power": 211767.65,
            "excess_liq": 57618.34,
            "maint_margin": 28429.05,
            "accrued": 0.0,
            "cushion": 0.524198,
        },
    )
    monkeypatch.setattr(
        bot,
        "_load_submitted_unfilled_orders",
        lambda: [
            {
                "symbol": "PLTR",
                "action": "SELL",
                "quantity": 1,
                "limit_price": 3.06,
                "status": "Submitted",
                "filled": 0,
                "remaining": 1,
                "order_id": 544,
                "created_at": "2026-06-08 16:20:07",
            }
        ],
    )

    msg = bot._format_db_status_fallback(ConnectionRefusedError("offline"))

    assert "IBKR live connection unavailable" in msg
    assert "Showing latest local DB snapshot" in msg
    assert "Run: <code>40</code>" in msg
    assert "PLTR" in msg
    assert "remaining 1" in msg


def test_revenue_monthly_ledger_splits_realized_and_open(monkeypatch) -> None:
    monkeypatch.setattr(
        bot,
        "config",
        SimpleNamespace(
            telegram=SimpleNamespace(revenue_start_date="2026-03-01"),
            portfolio=SimpleNamespace(symbols={"NVDA": object(), "PLTR": object(), "TSLA": object()}),
        ),
    )

    rows = [
        SimpleNamespace(exec_id="import-leaps-pltr", symbol="PLTR", side="BOT", shares=1, price=57.75, execution_time="2026-03-02 00:00:00", sec_type=None, realized_pnl=None),
        SimpleNamespace(exec_id="import-nvda-open-old", symbol="NVDA", side="SLD", shares=2, price=2.44, execution_time="2026-03-05 00:00:00", sec_type=None, realized_pnl=None),
        # realized_pnl = IBKR net P&L on this close (cost basis $244 - buyback $305 = -$61 on 1 contract,
        # but original 2-contract lot had avg $244; IBKR reports net $183 = $244*1 - $3.05*100*... scenario test value)
        SimpleNamespace(exec_id="import-nvda-close-one", symbol="NVDA", side="BOT", shares=1, price=3.05, execution_time="2026-03-17 00:00:00", sec_type=None, realized_pnl=183.0),
        SimpleNamespace(exec_id="live-nvda-open", symbol="NVDA", side="SLD", shares=1, price=5.64, execution_time="2026-06-05 15:14:36", sec_type="OPT", realized_pnl=None),
        SimpleNamespace(exec_id="sgov-stock", symbol="SGOV", side="SLD", shares=90, price=100.46, execution_time="2026-06-08 16:21:15", sec_type="STK", realized_pnl=None),
    ]
    portfolio = [
        SimpleNamespace(
            contract=SimpleNamespace(secType="OPT", symbol="NVDA", lastTradeDateOrContractMonth="20260702"),
            position=-1,
        ),
    ]

    msg = bot._format_revenue_message(rows, portfolio, data_source="test")

    assert "2026-03" in msg
    assert "183.00" in msg
    assert "2026-07" in msg
    assert "564.00" in msg
    assert "Realized Total:</b> $183.00" in msg
    assert "Realized Avg/mo:</b> $183.00" in msg
    assert "Open / Not Recognized Premium:</b> $564.00" in msg
    assert "Month    Realized     Open/Pending\n" in msg
    assert "Cashflow Total" not in msg
    assert "747.00" not in msg
    assert "SGOV" not in msg


def test_revenue_db_fallback_uses_local_history_and_position_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(
        bot,
        "config",
        SimpleNamespace(
            telegram=SimpleNamespace(revenue_start_date="2026-03-01"),
            portfolio=SimpleNamespace(symbols={"NVDA": object()}),
        ),
    )
    monkeypatch.setattr(
        bot,
        "_load_latest_position_snapshot",
        lambda: [
            SimpleNamespace(
                contract=SimpleNamespace(
                    secType="OPT",
                    symbol="NVDA",
                    lastTradeDateOrContractMonth="20260702",
                ),
                position=-1,
                averageCost=564.0,
            )
        ],
    )

    rows = [
        SimpleNamespace(
            exec_id="live-nvda-open",
            symbol="NVDA",
            side="SLD",
            shares=1,
            price=5.64,
            execution_time="2026-06-05 15:14:36",
            sec_type="OPT",
        )
    ]

    msg = bot._format_revenue_db_fallback(ConnectionRefusedError("offline"), rows)

    assert "IBKR live connection unavailable" in msg
    assert "Showing latest local DB revenue snapshot" in msg
    assert "local historical DB snapshot" in msg
    assert "Open / Not Recognized Premium:</b> $564.00" in msg
    assert "Error fetching option revenue" not in msg


def test_load_unnotified_today_order_fills_excludes_notified_exec_ids(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "state.db"
    config_path = str(tmp_path / "thetagang.toml")
    data_store = DataStore(
        f"sqlite:///{db_path}",
        config_path,
        dry_run=False,
        config_text="test",
    )

    contract = SimpleNamespace(
        symbol="PLTR",
        secType="OPT",
        conId=885469958,
        exchange="SMART",
        currency="USD",
    )
    order = SimpleNamespace(
        action="SELL",
        totalQuantity=1,
        lmtPrice=2.86,
        orderType="LMT",
        orderRef="",
        tif="DAY",
        orderId=732,
    )
    data_store.record_order(contract, order)
    data_store.record_executions(
        [
            SimpleNamespace(
                execution=SimpleNamespace(
                    execId="pltr-fill-1",
                    orderId=732,
                    orderRef="",
                    side="SLD",
                    shares=1,
                    price=2.86,
                    exchange="SMART",
                    time="20260609 13:55:00",
                ),
                contract=contract,
                time="20260609 13:55:00",
            )
        ]
    )

    monkeypatch.setattr(bot, "config_path", config_path)
    monkeypatch.setattr(
        bot,
        "config",
        SimpleNamespace(
            runtime=SimpleNamespace(database=_DatabaseConfig(f"sqlite:///{db_path}"))
        ),
    )
    monkeypatch.setattr(
        bot,
        "_taipei_today_utc_window",
        lambda: (
            bot.datetime.now() - bot.timedelta(days=1),
            bot.datetime.now() + bot.timedelta(days=1),
        ),
    )

    rows = bot._load_unnotified_today_order_fills([])
    assert len(rows) == 1
    assert rows[0]["exec_id"] == "pltr-fill-1"
    assert rows[0]["order_sec_type"] == "OPT"

    assert bot._load_unnotified_today_order_fills(["pltr-fill-1"]) == []


def test_format_fill_monitor_message_shows_option_premium() -> None:
    msg = bot._format_fill_monitor_message(
        [
            {
                "exec_id": "pltr-fill-1",
                "order_id": 732,
                "symbol": "PLTR",
                "side": "SLD",
                "shares": 1,
                "price": 2.86,
                "execution_time": "2026-06-09 13:55:00",
                "order_sec_type": "OPT",
            }
        ]
    )

    assert "ThetaGang 當日下單已成交" in msg
    assert "PLTR" in msg
    assert "premium $286.00" in msg
    assert "order 732" in msg


@pytest.mark.asyncio
async def test_trades_command_lists_only_last_three_days(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "state.db"
    config_path = str(tmp_path / "thetagang.toml")
    data_store = DataStore(
        f"sqlite:///{db_path}",
        config_path,
        dry_run=False,
        config_text="test",
    )
    contract = SimpleNamespace(symbol="PLTR")
    data_store.record_executions(
        [
            SimpleNamespace(
                execution=SimpleNamespace(
                    execId="old-fill",
                    orderId=1,
                    orderRef="",
                    side="SLD",
                    shares=1,
                    price=1.23,
                    exchange="SMART",
                    time="20260606 11:59:59",
                ),
                contract=contract,
                time="20260606 11:59:59",
            ),
            SimpleNamespace(
                execution=SimpleNamespace(
                    execId="recent-fill",
                    orderId=2,
                    orderRef="",
                    side="BOT",
                    shares=2,
                    price=2.34,
                    exchange="SMART",
                    time="20260609 12:00:00",
                ),
                contract=contract,
                time="20260609 12:00:00",
            ),
        ]
    )

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 10, 12, 0, 0)

    class StatusMessage:
        text = ""

        async def edit_text(self, text, **_kwargs):
            self.text = text

    status_msg = StatusMessage()

    class Message:
        async def reply_text(self, _text):
            return status_msg

    monkeypatch.setattr(bot, "datetime", FixedDateTime)
    monkeypatch.setattr(bot, "is_authorized", lambda _chat_id: True)
    monkeypatch.setattr(bot, "config_path", config_path)
    monkeypatch.setattr(
        bot,
        "config",
        SimpleNamespace(
            runtime=SimpleNamespace(database=_DatabaseConfig(f"sqlite:///{db_path}"))
        ),
    )

    update: Any = SimpleNamespace(effective_chat=SimpleNamespace(id=123), message=Message())
    context: Any = SimpleNamespace()

    await bot.trades_command(update, context)

    assert "Last 3 Days" in status_msg.text
    assert "recent-fill" not in status_msg.text
    assert "PLTR" in status_msg.text
    assert "06-09 12:00" in status_msg.text
    assert "06-06 11:59" not in status_msg.text
