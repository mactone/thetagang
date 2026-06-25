#!/usr/bin/env python3
"""Import historical IBKR option trades into ThetaGang's executions table.

Supported inputs:
- IBKR Flex XML containing <Trade ...> rows
- IBKR Activity/Flex CSV containing trade rows

Only option rows are imported. The existing /revenue command treats every row
in executions as an option execution and computes premium cashflow as:
SLD + quantity * price * 100; BOT - quantity * price * 100.

Usage:
  python scripts/import_ibkr_option_executions.py --file /path/report.xml --db data/thetagang.db --dry-run
  python scripts/import_ibkr_option_executions.py --file /path/report.csv --db data/thetagang.db
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

OPTION_CATEGORIES = {"OPT", "FOP", "OPTIONS", "OPTION"}
BUY_WORDS = {"BUY", "BOT", "B", "買"}
SELL_WORDS = {"SELL", "SLD", "S", "賣"}


@dataclass(frozen=True)
class ImportedExecution:
    exec_id: str
    order_id: int | None
    order_ref: str | None
    symbol: str
    side: str
    shares: float
    price: float
    execution_time: str
    exchange: str | None


def norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def pick(row: dict[str, str], *names: str) -> str:
    for name in names:
        if name in row:
            return (row[name] or "").strip()
    normalized = {norm_key(k): v for k, v in row.items() if k is not None and norm_key(k)}
    for name in names:
        key = norm_key(name)
        if key and key in normalized:
            return (normalized[key] or "").strip()
    return ""


def parse_float(value: str) -> float:
    value = (value or "").strip().replace(",", "")
    value = value.replace("$", "")
    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1]
    return abs(float(value)) if value else 0.0


def parse_int(value: str) -> int | None:
    value = (value or "").strip().replace(",", "")
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def parse_time(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("missing execution time")
    raw = raw.replace("T", " ")
    raw = re.sub(r"\s+[A-Z]{2,4}$", "", raw)
    raw = raw.split("+")[0].split("Z")[0]
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d;%H:%M:%S",
        "%Y%m%d %H:%M:%S",
        "%Y%m%d  %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%y %H:%M:%S",
        "%Y-%m-%d",
        "%Y%m%d",
        "%m/%d/%Y",
        "%m/%d/%y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    raise ValueError(f"unsupported execution time format: {value!r}")


def parse_side(value: str) -> str:
    token = (value or "").strip().upper()
    if token in BUY_WORDS or token.startswith("BUY"):
        return "BOT"
    if token in SELL_WORDS or token.startswith("SELL"):
        return "SLD"
    raise ValueError(f"unsupported side: {value!r}")


def is_option_row(row: dict[str, str]) -> bool:
    cat = pick(row, "assetCategory", "asset category", "assetClass", "asset class", "secType", "security type", "type")
    if cat.upper() in OPTION_CATEGORIES:
        return True
    code = pick(row, "code", "代碼", "symbol")
    if re.search(r"\b[A-Z]{1,6}\s+\d{6}[CP]\d{8}\b", code):
        return True
    desc = " ".join([
        pick(row, "description", "securityDescription", "security description", "說明"),
        pick(row, "putCall", "put/call", "right"),
        pick(row, "expiry", "expiration", "lastTradeDateOrContractMonth"),
    ]).upper()
    return bool(re.search(r"\b(CALL|PUT)\b", desc) or re.search(r"\b\d{2}[A-Z]{3}\d{2}\s+[0-9.]+\s+[CP]\b", desc) or pick(row, "strike", "strikePrice"))


def make_exec_id(row: dict[str, str], symbol: str, side: str, shares: float, price: float, execution_time: str) -> str:
    raw_exec_id = pick(row, "ibExecID", "ibExecId", "execId", "executionId", "execution id", "tradeID", "trade id")
    if raw_exec_id:
        return raw_exec_id
    basis = "|".join([
        pick(row, "_source_row_index"),
        execution_time,
        symbol,
        side,
        f"{shares:.8f}",
        f"{price:.8f}",
        pick(row, "orderID", "order id", "ibOrderID"),
        pick(row, "conid", "conId"),
    ])
    return "import-" + hashlib.sha256(basis.encode()).hexdigest()[:24]


def row_to_execution(row: dict[str, str]) -> ImportedExecution | None:
    if not is_option_row(row):
        return None
    symbol = pick(row, "symbol", "underlyingSymbol", "underlying symbol", "rootSymbol", "root symbol")
    code = pick(row, "code", "代碼")
    if not symbol and code:
        symbol = code.split()[0]
    if not symbol:
        symbol = pick(row, "description", "securityDescription", "security description", "說明").split()[0]
    if not symbol:
        raise ValueError(f"missing symbol in row: {row}")
    side = parse_side(pick(row, "buySell", "buy/sell", "side", "transactionType", "transaction type", "交易類型"))
    shares = parse_float(pick(row, "quantity", "qty", "shares", "交易量"))
    price = parse_float(pick(row, "tradePrice", "trade price", "price", "價格"))
    execution_time = parse_time(pick(row, "dateTime", "date/time", "tradeDate", "trade date", "date", "reportDate", "日期"))
    return ImportedExecution(
        exec_id=make_exec_id(row, symbol, side, shares, price, execution_time),
        order_id=parse_int(pick(row, "orderID", "order id", "ibOrderID")),
        order_ref=pick(row, "orderRef", "order ref") or None,
        symbol=symbol.upper(),
        side=side,
        shares=shares,
        price=price,
        execution_time=execution_time,
        exchange=pick(row, "exchange", "listingExchange") or None,
    )


def read_xml(path: Path) -> Iterable[dict[str, str]]:
    root = ET.parse(path).getroot()
    for elem in root.iter():
        if elem.tag.split("}")[-1].lower() == "trade":
            yield dict(elem.attrib)


def read_csv(path: Path) -> Iterable[dict[str, str]]:
    # IBKR CSV exports can be sectioned: Section,Header,... followed by Section,Data,...
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    parsed = [next(csv.reader([line])) for line in lines if line.strip()]
    for idx, cols in enumerate(parsed):
        if len(cols) >= 4 and cols[1].strip().lower() == "header" and ("日期" in cols or "Date" in cols):
            section = cols[0]
            headers = cols[2:]
            for source_idx, data_cols in enumerate(parsed[idx + 1 :], start=idx + 2):
                if len(data_cols) < 2 or data_cols[0] != section:
                    continue
                if data_cols[1].strip().lower() != "data":
                    continue
                values = data_cols[2:]
                row = dict(zip(headers, values))
                row["_source_row_index"] = str(source_idx)
                yield row
            return

    # Standard CSV: find a row with enough trade-like columns.
    for start, line in enumerate(lines):
        cols = [norm_key(c) for c in next(csv.reader([line]))]
        if {"symbol", "quantity"}.issubset(cols) and ({"buysell", "side"} & set(cols)):
            reader = csv.DictReader(lines[start:])
            for source_idx, row in enumerate(reader, start=start + 2):
                row["_source_row_index"] = str(source_idx)
                yield row
            return
    # Fallback: assume first line is header.
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.DictReader(fh)
        for source_idx, row in enumerate(reader, start=2):
            row["_source_row_index"] = str(source_idx)
            yield row


def load_rows(path: Path) -> list[ImportedExecution]:
    raw_rows = read_xml(path) if path.suffix.lower() in {".xml", ".flex"} else read_csv(path)
    rows: list[ImportedExecution] = []
    errors: list[str] = []
    for idx, row in enumerate(raw_rows, start=1):
        try:
            ex = row_to_execution(row)
            if ex:
                rows.append(ex)
        except Exception as exc:
            errors.append(f"row {idx}: {exc}")
    if errors:
        print("WARN: skipped rows with parse errors:", file=sys.stderr)
        for msg in errors[:20]:
            print(f"  - {msg}", file=sys.stderr)
        if len(errors) > 20:
            print(f"  ... {len(errors) - 20} more", file=sys.stderr)
    return rows


def ensure_import_run(conn: sqlite3.Connection, config_path: str) -> int:
    config_text = Path(config_path).read_text(encoding="utf-8") if Path(config_path).exists() else None
    conn.execute(
        "insert into runs (started_at, config_path, dry_run, version, hostname, config_text) values (?, ?, ?, ?, ?, ?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), config_path, 1, "ibkr-flex-import", os.uname().nodename, config_text),
    )
    return int(conn.execute("select last_insert_rowid()").fetchone()[0])


def insert_rows(conn: sqlite3.Connection, rows: list[ImportedExecution], run_id: int) -> int:
    inserted = 0
    for ex in rows:
        cur = conn.execute(
            """
            insert or ignore into executions
              (run_id, created_at, exec_id, order_id, order_ref, symbol, side, shares, price, execution_time, exchange)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ex.exec_id,
                ex.order_id,
                ex.order_ref,
                ex.symbol,
                ex.side,
                ex.shares,
                ex.price,
                ex.execution_time,
                ex.exchange,
            ),
        )
        inserted += cur.rowcount
    return inserted


def summarize(rows: list[ImportedExecution]) -> str:
    by_month: dict[str, float] = defaultdict(float)
    by_symbol: dict[str, float] = defaultdict(float)
    sides = Counter()
    for ex in rows:
        sign = 1 if ex.side == "SLD" else -1
        cashflow = sign * ex.shares * ex.price * 100
        month = ex.execution_time[:7]
        by_month[month] += cashflow
        by_symbol[ex.symbol] += cashflow
        sides[ex.side] += 1
    lines = [f"option rows parsed: {len(rows)}", f"sides: {dict(sides)}"]
    lines.append("monthly premium cashflow (SLD +, BOT -; excludes fees):")
    for month in sorted(by_month):
        lines.append(f"  {month}: {by_month[month]:,.2f}")
    lines.append("top symbols:")
    for symbol, amount in sorted(by_symbol.items(), key=lambda kv: abs(kv[1]), reverse=True)[:10]:
        lines.append(f"  {symbol}: {amount:,.2f}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, type=Path, help="IBKR Flex XML or Activity/Flex CSV file")
    parser.add_argument("--db", default="data/thetagang.db", type=Path)
    parser.add_argument("--config", default="thetagang.toml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    if not args.file.exists():
        raise SystemExit(f"input file not found: {args.file}")
    if not args.db.exists():
        raise SystemExit(f"database not found: {args.db}")

    rows = load_rows(args.file)
    print(summarize(rows))
    if args.dry_run:
        print("DRY RUN: database unchanged")
        return 0
    if not rows:
        print("No option rows to import; database unchanged")
        return 0

    if not args.no_backup:
        backup = args.db.with_suffix(args.db.suffix + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(args.db, backup)
        print(f"backup written: {backup}")

    with sqlite3.connect(args.db) as conn:
        run_id = ensure_import_run(conn, args.config)
        inserted = insert_rows(conn, rows, run_id)
        conn.commit()
    print(f"inserted executions: {inserted}; duplicates skipped: {len(rows) - inserted}; run_id: {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
