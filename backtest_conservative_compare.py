#!/usr/bin/env python3
from pathlib import Path
import os, math, json
import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib.pyplot as plt
import matplotlib as mpl
from scipy.stats import norm
from scipy.optimize import brentq

OUT_DIR=Path(os.environ.get('THETAGANG_OUT_DIR', './output/backtest')); OUT_DIR.mkdir(parents=True, exist_ok=True)
START='2016-01-01'; INITIAL_NAV=100000.0; IV_MULT=1.20
SCENARIOS={
 'current': {'symbols': {'TSLA':0.50,'PLTR':0.10,'NVDA':0.40}, 'margin_usage':0.60,'delta':0.30,'dte':21},
 # 牛市末/初跌段防守版：低 Delta、低部署、ETF 防禦為主，現金流明顯下降但回撤小。
 'conservative': {'symbols': {'SPY':0.45,'XLV':0.20,'XLP':0.15,'NVDA':0.20}, 'margin_usage':0.30,'delta':0.15,'dte':35},
 # 折衷現金流版：仍降低 Delta/槓桿，但保留少量高 IV 標的以維持收入。
 'defensive_income': {'symbols': {'SPY':0.35,'NVDA':0.25,'TSLA':0.15,'XLV':0.15,'XLP':0.10}, 'margin_usage':0.40,'delta':0.16,'dte':35},
}

def download():
    tickers=sorted(set(sum([list(s['symbols'].keys()) for s in SCENARIOS.values()],[])+['SPY','^IRX']))
    raw=yf.download(tickers,start=START,auto_adjust=True,progress=False,group_by='ticker',threads=True)
    closes=pd.DataFrame({t: raw[t]['Close'] for t in tickers}).sort_index()
    return closes.dropna(how='all'), closes['^IRX'].ffill().fillna(0)/100, closes['SPY'].ffill()

def put_price(s,k,t,r,sigma):
    if sigma<=1e-9 or t<=0: return max(k-s,0)
    d1=(math.log(s/k)+(r+0.5*sigma*sigma)*t)/(sigma*math.sqrt(t)); d2=d1-sigma*math.sqrt(t)
    return k*math.exp(-r*t)*norm.cdf(-d2)-s*norm.cdf(-d1)

def strike_delta(s,t,r,sigma,delta):
    def f(k):
        d1=(math.log(s/k)+(r+0.5*sigma*sigma)*t)/(sigma*math.sqrt(t))
        return abs(norm.cdf(d1)-1)-delta
    try: return brentq(f,0.2*s,2*s)
    except Exception: return s*(0.92 if delta>=0.25 else 0.85)

def run(closes, irx, cfg):
    symbols=cfg['symbols']; prices=closes[list(symbols)]
    vol=prices.pct_change().rolling(30).std()*math.sqrt(252)*IV_MULT
    nav=INITIAL_NAV; rows=[]; trades=[]; dates=prices.index; i=31; dte=cfg['dte']
    while i+dte<len(dates):
        entry=dates[i]; expiry=dates[i+dte]; avg_rf=float(irx.loc[entry:expiry].mean()) if not irx.loc[entry:expiry].empty else 0
        cash_nav=nav*(1-cfg['margin_usage']); untrade=0; pnl=0; days=max((expiry-entry).days,1)
        for sym,w in symbols.items():
            s0=prices.at[entry,sym]; st=prices.at[expiry,sym]; sig=vol.at[entry,sym]; notional=nav*cfg['margin_usage']*w
            if not(np.isfinite(s0) and np.isfinite(st) and np.isfinite(sig) and sig>0): untrade+=notional; continue
            t=dte/252; k=strike_delta(float(s0),t,avg_rf,float(sig),cfg['delta']); prem=put_price(float(s0),k,t,avg_rf,float(sig)); payoff=-max(k-float(st),0)
            p=notional/k*(prem+payoff); pnl+=p
            trades.append({'entry':entry,'expiry':expiry,'symbol':sym,'pnl_pct_notional':p/notional,'assigned':float(st)<k})
        interest=(cash_nav+untrade)*avg_rf*days/365
        nav+=pnl+interest; rows.append({'date':expiry,'nav':nav,'cycle_pnl':pnl,'cash_interest':interest})
        i+=dte
    nav_df=pd.DataFrame(rows).set_index('date'); tr=pd.DataFrame(trades)
    annual=nav_df['nav'].resample('YE').last().to_frame('ending_nav'); prev=annual['ending_nav'].shift(1); prev.iloc[0]=INITIAL_NAV
    annual['return_pct']=(annual['ending_nav']/prev-1)*100; annual['year']=annual.index.year
    annual['annual_gain_usd']=annual['ending_nav']-prev; annual['quarterly_equiv_usd']=annual['annual_gain_usd']/4
    yrs=(nav_df.index[-1]-nav_df.index[0]).days/365.25; cagr=(nav_df.nav.iloc[-1]/INITIAL_NAV)**(1/yrs)-1; dd=(nav_df.nav/nav_df.nav.cummax()-1).min()
    summary={'ending_nav':float(nav_df.nav.iloc[-1]),'cagr_pct':float(cagr*100),'max_drawdown_pct':float(dd*100),'assignment_rate_pct':float(tr.assigned.mean()*100),'trade_count':len(tr)}
    return nav_df, annual[['year','ending_nav','return_pct','annual_gain_usd','quarterly_equiv_usd']], tr, summary

def main():
    closes,irx,spy=download(); results={}
    for name,cfg in SCENARIOS.items():
        results[name]=run(closes,irx,cfg)
    # plot compare
    mpl.rcParams['font.family']=['Arial Unicode MS','Heiti TC','Hiragino Sans','DejaVu Sans']; mpl.rcParams['axes.unicode_minus']=False
    fig,ax=plt.subplots(figsize=(12,7),dpi=180)
    colors={'current':'#2563eb','conservative':'#16a34a','defensive_income':'#8b5cf6'}
    for name,(nav,annual,tr,summary) in results.items(): ax.plot(nav.index,nav.nav,label=f"{name}: CAGR {summary['cagr_pct']:.1f}%, MaxDD {summary['max_drawdown_pct']:.1f}%",lw=2.3,color=colors[name])
    base=results['current'][0].index
    spy_a=spy.reindex(base,method='ffill').dropna(); spy_nav=spy_a/spy_a.iloc[0]*INITIAL_NAV
    ax.plot(spy_nav.index,spy_nav,label='SPY buy & hold（歸一化）',lw=2,color='#f97316')
    ax.axhline(INITIAL_NAV,color='#64748b',ls='--',lw=1); ax.legend(); ax.grid(True,alpha=.3)
    ax.set_title('ThetaGang 現有參數 vs 較保守參數 vs SPY',fontsize=16,weight='bold'); ax.set_ylabel('NAV / normalized $')
    out=OUT_DIR/'thetagang_current_vs_conservative.png'; fig.savefig(out,bbox_inches='tight'); plt.close(fig)
    combined=[]
    for name,(nav,annual,tr,summary) in results.items():
        a=annual.copy(); a.insert(0,'scenario',name); combined.append(a)
        annual.to_csv(OUT_DIR/f'annual_returns_{name}.csv',index=False); nav.to_csv(OUT_DIR/f'nav_curve_{name}.csv')
    comp=pd.concat(combined); comp.to_csv(OUT_DIR/'annual_returns_current_vs_conservative.csv',index=False)
    summaries={name:res[3] for name,res in results.items()}; (OUT_DIR/'summary_current_vs_conservative.json').write_text(json.dumps(summaries,ensure_ascii=False,indent=2),encoding='utf-8')
    print('CHART',out)
    print(json.dumps(summaries,ensure_ascii=False,indent=2))
    print('ANNUAL_COMPARE')
    print(comp.to_string(index=False,formatters={'ending_nav':'{:,.0f}'.format,'return_pct':'{:.2f}'.format,'annual_gain_usd':'{:,.0f}'.format,'quarterly_equiv_usd':'{:,.0f}'.format}))
if __name__=='__main__': main()
