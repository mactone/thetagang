#!/usr/bin/env python3
"""
Simplified 10-year ThetaGang current-parameter backtest.

Read-only analytical model. It does NOT connect to IBKR and does NOT place orders.
Assumptions:
- Current repo config observed manually: deployed option notional = 60% NAV, symbols TSLA 50%, PLTR 10%, NVDA 40%.
- Wheel target: 21 trading-day cycles, 0.30 delta short puts/calls.
- Premium estimated with Black-Scholes using trailing 30-trading-day realized volatility * 1.20 IV premium proxy.
- Fractional contracts are allowed for clean portfolio-level history.
- Missing PLTR before listing is kept as cash.
- Idle cash earns daily ^IRX 13-week T-bill proxy when available; otherwise 0%.
- Assignment model: short put payoff at cycle expiry; no intra-cycle margin calls/slippage/taxes/commissions.
This is a parameter stress/backtest approximation, not broker-certified P&L.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import math
import json

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib.pyplot as plt
import matplotlib as mpl
from scipy.stats import norm
from scipy.optimize import brentq

# macOS Traditional Chinese font; avoids tofu boxes in chart titles/notes.
mpl.rcParams['font.family'] = ['Arial Unicode MS', 'Heiti TC', 'Hiragino Sans', 'DejaVu Sans']
mpl.rcParams['axes.unicode_minus'] = False

OUT_DIR = Path(os.environ.get('THETAGANG_OUT_DIR', './output/backtest'))
OUT_DIR.mkdir(parents=True, exist_ok=True)

START = '2016-01-01'
END = None
SYMBOLS = {'TSLA': 0.50, 'PLTR': 0.10, 'NVDA': 0.40}
MARGIN_USAGE = 0.60
TARGET_DELTA = 0.30
DTE = 21
IV_MULTIPLIER = 1.20
INITIAL_NAV = 100_000.0

@dataclass
class Trade:
    entry: pd.Timestamp
    expiry: pd.Timestamp
    symbol: str
    side: str
    s0: float
    st: float
    strike: float
    premium: float
    pnl_pct_notional: float
    assigned: bool


def download_prices() -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    tickers = list(SYMBOLS) + ['SPY', '^IRX']
    raw = yf.download(tickers, start=START, end=END, auto_adjust=True, progress=False, group_by='ticker', threads=True)
    closes = pd.DataFrame()
    for t in tickers:
        try:
            closes[t] = raw[t]['Close']
        except Exception:
            closes[t] = raw['Close'] if len(tickers) == 1 else np.nan
    closes = closes.sort_index()
    prices = closes[list(SYMBOLS)]
    spy = closes['SPY'].ffill()
    # ^IRX is quoted as annualized discount yield percent. Use conservative percent/100.
    irx = closes['^IRX'].ffill().fillna(0) / 100.0
    return prices, irx, spy


def bs_put_price(s, k, t, r, sigma):
    if sigma <= 1e-9 or t <= 0:
        return max(k - s, 0.0)
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    return k * math.exp(-r * t) * norm.cdf(-d2) - s * norm.cdf(-d1)


def bs_call_price(s, k, t, r, sigma):
    if sigma <= 1e-9 or t <= 0:
        return max(s - k, 0.0)
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    return s * norm.cdf(d1) - k * math.exp(-r * t) * norm.cdf(d2)


def strike_for_abs_delta(s, t, r, sigma, abs_delta, option_type):
    # Solve in [0.2S, 2.0S]. Put delta = N(d1)-1, call delta = N(d1).
    def f(k):
        d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
        if option_type == 'put':
            return abs(norm.cdf(d1) - 1.0) - abs_delta
        return norm.cdf(d1) - abs_delta
    lo, hi = 0.2 * s, 2.0 * s
    try:
        return brentq(f, lo, hi, maxiter=100)
    except ValueError:
        # Fallback approximate OTM strike.
        return s * (0.92 if option_type == 'put' else 1.08)


def run_backtest(prices: pd.DataFrame, irx: pd.Series):
    ret = prices.pct_change()
    realized_vol = ret.rolling(30).std() * math.sqrt(252) * IV_MULTIPLIER
    nav = INITIAL_NAV
    nav_rows = []
    trades: list[Trade] = []
    dates = prices.index
    # Rebalance every DTE trading days from first date with enough vol history.
    start_idx = max(31, 0)
    i = start_idx
    while i + DTE < len(dates):
        entry = dates[i]
        expiry = dates[i + DTE]
        # cash accrual on idle 40% and untradeable missing allocations
        cycle_days = max((expiry - entry).days, 1)
        avg_rf = float(irx.loc[entry:expiry].mean()) if not irx.loc[entry:expiry].empty else 0.0
        cash_nav = nav * (1 - MARGIN_USAGE)
        option_cycle_pnl = 0.0
        untradeable_cash = 0.0
        for sym, weight in SYMBOLS.items():
            s0 = prices.at[entry, sym]
            st = prices.at[expiry, sym]
            sigma = realized_vol.at[entry, sym]
            alloc_notional = nav * MARGIN_USAGE * weight
            if not (np.isfinite(s0) and np.isfinite(st) and np.isfinite(sigma) and sigma > 0):
                untradeable_cash += alloc_notional
                continue
            r = avg_rf
            t = DTE / 252.0
            # Current config writes both puts and calls at 0.30 delta when covered/assigned;
            # for a clean portfolio test, model the core cash-secured put premium engine.
            k = strike_for_abs_delta(float(s0), t, r, float(sigma), TARGET_DELTA, 'put')
            premium = bs_put_price(float(s0), k, t, r, float(sigma))
            payoff = -max(k - float(st), 0.0)
            pnl_per_share = premium + payoff
            pnl = alloc_notional / k * pnl_per_share
            option_cycle_pnl += pnl
            trades.append(Trade(entry, expiry, sym, 'short_put', float(s0), float(st), k, premium, pnl / alloc_notional, float(st) < k))
        cash_interest = (cash_nav + untradeable_cash) * avg_rf * cycle_days / 365.0
        nav = nav + option_cycle_pnl + cash_interest
        nav_rows.append({'date': expiry, 'nav': nav, 'cycle_pnl': option_cycle_pnl, 'cash_interest': cash_interest})
        i += DTE
    nav_df = pd.DataFrame(nav_rows).set_index('date')
    return nav_df, pd.DataFrame([t.__dict__ for t in trades])


def summarize(nav_df: pd.DataFrame, trades_df: pd.DataFrame):
    annual = nav_df['nav'].resample('YE').last().to_frame('ending_nav')
    # Include initial value at start for first annual pct.
    prev = annual['ending_nav'].shift(1)
    if not annual.empty:
        prev.iloc[0] = INITIAL_NAV
    annual['return_pct'] = (annual['ending_nav'] / prev - 1) * 100
    annual['year'] = annual.index.year
    annual = annual[['year', 'ending_nav', 'return_pct']]
    total_return = nav_df['nav'].iloc[-1] / INITIAL_NAV - 1
    years = (nav_df.index[-1] - nav_df.index[0]).days / 365.25
    cagr = (nav_df['nav'].iloc[-1] / INITIAL_NAV) ** (1 / years) - 1
    running_max = nav_df['nav'].cummax()
    max_dd = (nav_df['nav'] / running_max - 1).min()
    summary = {
        'initial_nav': INITIAL_NAV,
        'ending_nav': float(nav_df['nav'].iloc[-1]),
        'total_return_pct': float(total_return * 100),
        'cagr_pct': float(cagr * 100),
        'max_drawdown_pct': float(max_dd * 100),
        'trade_count': int(len(trades_df)),
        'assignment_rate_pct': float(trades_df['assigned'].mean() * 100),
        'assumptions': {
            'symbols_weights': SYMBOLS,
            'margin_usage': MARGIN_USAGE,
            'target_delta': TARGET_DELTA,
            'dte_trading_days': DTE,
            'iv_proxy': '30D realized volatility * 1.20',
            'cash_proxy': '^IRX 13-week T-bill yield, missing cash at 0%',
            'model': 'cash-secured short put engine, fractional contracts, no taxes/commissions/slippage/intraday margin calls',
        },
    }
    return annual, summary


def plot(nav_df, annual, summary, spy):
    plt.style.use('seaborn-v0_8-whitegrid')
    mpl.rcParams['font.family'] = ['Arial Unicode MS', 'Heiti TC', 'Hiragino Sans', 'DejaVu Sans']
    mpl.rcParams['axes.unicode_minus'] = False
    fig = plt.figure(figsize=(12, 9), dpi=180)
    gs = fig.add_gridspec(2, 1, height_ratios=[1.05, 1.0], hspace=0.28)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1])
    ax1.plot(nav_df.index, nav_df['nav'], color='#2563eb', linewidth=2.4, label='ThetaGang NAV')
    ax1.fill_between(nav_df.index, nav_df['nav'], INITIAL_NAV, color='#93c5fd', alpha=0.20)
    ax1.axhline(INITIAL_NAV, color='#64748b', linestyle='--', linewidth=1)

    spy_aligned = spy.reindex(nav_df.index, method='ffill').dropna()
    if not spy_aligned.empty:
        spy_nav = spy_aligned / spy_aligned.iloc[0] * INITIAL_NAV
        ax1.plot(spy_nav.index, spy_nav, color='#f97316', linewidth=2.0, linestyle='-', alpha=0.92, label='SPY buy & hold（歸一化）')
        spy_total = spy_nav.iloc[-1] / INITIAL_NAV - 1
        txt_spy = f"SPY 同期總報酬 {spy_total * 100:.1f}%"
    else:
        txt_spy = 'SPY 資料不足'

    ax1.legend(loc='upper left', frameon=True)
    ax1.set_title('ThetaGang 現有參數 vs 大盤 SPY：10 年簡化回測', fontsize=16, weight='bold')
    ax1.set_ylabel('Portfolio NAV / SPY normalized ($)')
    txt = f"ThetaGang Ending ${summary['ending_nav']:,.0f} | CAGR {summary['cagr_pct']:.1f}% | Max DD {summary['max_drawdown_pct']:.1f}%\n{txt_spy}"
    ax1.text(0.01, 0.84, txt, transform=ax1.transAxes, fontsize=11, va='top', bbox=dict(facecolor='white', alpha=0.85, edgecolor='#cbd5e1'))

    colors = ['#16a34a' if x >= 0 else '#dc2626' for x in annual['return_pct']]
    ax2.bar(annual['year'].astype(str), annual['return_pct'], color=colors, alpha=0.88)
    ax2.axhline(0, color='#334155', linewidth=1)
    ax2.set_title('年度收益變化（%）', fontsize=15, weight='bold')
    ax2.set_ylabel('Annual Return (%)')
    ax2.tick_params(axis='x', rotation=45)
    for idx, row in annual.iterrows():
        y = row['return_pct']
        ax2.text(str(int(row['year'])), y + (1.2 if y >= 0 else -2.2), f"{y:.1f}%", ha='center', va='bottom' if y >= 0 else 'top', fontsize=8)
    fig.text(0.01, 0.01, '模型限制：用歷史股價 + Black-Scholes 估 0.30 delta 權利金；非真實歷史 option chain；不含稅、佣金、滑價、盤中追繳。', fontsize=9, color='#475569')
    out = OUT_DIR / 'thetagang_current_params_10y_backtest.png'
    fig.savefig(out, bbox_inches='tight')
    plt.close(fig)
    return out


def main():
    prices, irx, spy = download_prices()
    nav_df, trades_df = run_backtest(prices, irx)
    annual, summary = summarize(nav_df, trades_df)
    chart = plot(nav_df, annual, summary, spy)
    nav_df.to_csv(OUT_DIR / 'nav_curve.csv')
    annual.to_csv(OUT_DIR / 'annual_returns.csv', index=False)
    trades_df.to_csv(OUT_DIR / 'trades.csv', index=False)
    (OUT_DIR / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print('CHART', chart)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print('\nANNUAL_RETURNS')
    print(annual.to_string(index=False, formatters={'ending_nav': '{:,.0f}'.format, 'return_pct': '{:.2f}'.format}))

if __name__ == '__main__':
    main()
