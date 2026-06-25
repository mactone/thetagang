#!/usr/bin/env python3
"""Run IB Gateway only for Telegram dashboard commands.

No PortfolioManager is imported/run, so this helper does not cancel, place,
roll, close, or adjust orders. It only starts IBC/IB Gateway and keeps API port
7497 available for the Telegram bot.
"""
from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import tomlkit
from ib_async import IBC

from thetagang.config import Config

CONFIG_PATH = Path("/etc/thetagang/thetagang.toml")


async def main() -> int:
    config_doc = tomlkit.parse(CONFIG_PATH.read_text()).unwrap()
    config = Config(**config_doc)
    ibc = IBC(1045, **config.runtime.ibc.to_dict())
    stop = asyncio.Event()

    def _stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    await ibc.startAsync()
    print("IB Gateway helper started; no trading strategy is running.", flush=True)
    try:
        await stop.wait()
    finally:
        await ibc.terminateAsync()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
