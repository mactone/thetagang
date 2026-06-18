#!/usr/bin/env python3
"""Persistent IBC/TWS daemon.

Starts IBC which launches IB Gateway/TWS and keeps it alive indefinitely via
Watchdog. The trading engine runs with --without-ibc (connecting to this
already-running TWS), and the Telegram bot can connect at any time on port 7497.

Usage:
    python scripts/ibc_daemon.py --config /etc/thetagang/thetagang.toml
"""
import argparse
import asyncio
import logging
import sys
from typing import cast, Any, Awaitable

import tomlkit
from ib_async import IBC, IB, Watchdog, Contract, util

log = logging.getLogger("ibc_daemon")
logging.basicConfig(level=logging.INFO, format="[ibc_daemon] %(message)s")


class _IBRunner:
    def run(self, awaitable: Awaitable[Any]) -> Any: ...


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/thetagang/thetagang.toml")
    args = parser.parse_args()

    from thetagang.config import Config

    with open(args.config) as f:
        raw = f.read()
    config = Config(**tomlkit.parse(raw).unwrap())

    ibc_cfg = config.runtime.ibc
    wdog_cfg = config.runtime.watchdog
    probe_cfg = wdog_cfg.probeContract

    ib = IB()
    ibc = IBC(1045, **ibc_cfg.to_dict())
    probe = Contract(
        secType=probe_cfg.secType,
        symbol=probe_cfg.symbol,
        currency=probe_cfg.currency,
        exchange=probe_cfg.exchange,
    )

    # clientId=98 — distinct from trading engine (1) and Telegram bot (99).
    wdog_kwargs = wdog_cfg.to_dict()
    wdog_kwargs["clientId"] = 98

    watchdog = Watchdog(ibc, ib, probeContract=probe, **wdog_kwargs)

    def on_started(_w: Watchdog) -> None:
        log.info(f"TWS ready on {wdog_cfg.host}:{wdog_cfg.port}")

    def on_stopped(_w: Watchdog) -> None:
        log.warning("TWS watchdog stopped — will attempt to restart")

    watchdog.startedEvent += on_started
    watchdog.stoppedEvent += on_stopped

    async def run_daemon() -> None:
        watchdog.start()
        log.info("IBC daemon running — TWS will be kept alive persistently")
        # Run forever; Watchdog handles all restarts internally.
        while True:
            await asyncio.sleep(3600)

    log.info("Starting")
    cast(_IBRunner, ib).run(run_daemon())


if __name__ == "__main__":
    main()
