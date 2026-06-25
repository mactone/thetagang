#!/usr/bin/env python3
from pathlib import Path
import os, math, json
import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm
from scipy.optimize import brentq

OUT_DIR=Path(os.environ.get('THETAGANG_OUT_DIR', './output/backtest')); OUT_DIR.mkdir(parents=True, exist_ok=True)
START='2016-01-01'; INITIAL_NAV=100000.0; IV_MULT=1.20
SCENARIOS={
 'current': {'symbols': {'TSLA':0.50,'PLTR':0.10,'NVDA':0.40}, 'margin_usage':0.60,'put_delta':0.30,'call_delta':0.30,'dte':21},
 'conservative': {'symbols': {'SPY':0.45,'XLV':0.20,'XLP':0.15,'NVDA':0.20}, 'margin_usage':0.30,'put_delta':0.15,'call_delta':0.25,'dte':35},
 'defensive_income': {'symbols': {'SPY':0.35,'NVDA':0.25,'TSLA':0.15,'XLV':0.15,'XLP':0.10}, 'margin_usage':0.40,'put_delta':0.16,'call_delta':0.25,'dte':35},
}

def download():
    tickers=sorted(set(sum([list(s['symbols'].keys()) for s in SCENARIOS.values()],[])+['^IRX']))
    raw=yf.download(tickers,start=START,auto_adjust=True,progress=False,group_by='ticker',threads=True)
    closes=pd.DataFrame({t: raw[t]['Close'] for t in tickers}).sort_index()
    return closes.dropna(how='all'), closes['^IRX'].ffill().fillna(0)/100

def bs_price(s,k,t,r,sigma,kind):
    if sigma<=1e-9 or t<=0:
        return max(k-s,0) if kind=='put' else max(s-k,0)
    d1=(math.log(s/k)+(r+0.5*sigma*sigma)*t)/(sigma*math.sqrt(t)); d2=d1-sigma*math.sqrt(t)
    if kind=='put': return k*math.exp(-r*t)*norm.cdf(-d2)-s*norm.cdf(-d1)
    return s*norm.cdf(d1)-k*math.exp(-r*t)*norm.cdf(d2)

def strike_delta(s,t,r,sigma,delta,kind):
    def f(k):
        d1=(math.log(s/k)+(r+0.5*sigma*sigma)*t)/(sigma*math.sqrt(t))
        return (abs(norm.cdf(d1)-1) if kind=='put' else norm.cdf(d1))-delta
    try: return brentq(f,0.2*s,2*s)
    except Exception: return s*(0.92 if kind=='put' else 1.08)

def run(closes, irx, cfg):
    symbols=cfg['symbols']; prices=closes[list(symbols)]
    vol=prices.pct_change().rolling(30).std()*math.sqrt(252)*IV_MULT
    nav=INITIAL_NAV; dates=prices.index; i=31; dte=cfg['dte']; rows=[]
    while i+dte<len(dates):
        entry=dates[i]; expiry=dates[i+dte]
        avg_rf=float(irx.loc[entry:expiry].mean()) if not irx.loc[entry:expiry].empty else 0
        days=max((expiry-entry).days,1); t=dte/252
        put_prem=call_prem=put_payoff=call_payoff=interest=0.0; deployed=0.0
        cash_nav=nav*(1-cfg['margin_usage']); untrade=0.0
        for sym,w in symbols.items():
            s0=prices.at[entry,sym]; st=prices.at[expiry,sym]; sig=vol.at[entry,sym]
            notional=nav*cfg['margin_usage']*w
            if not(np.isfinite(s0) and np.isfinite(st) and np.isfinite(sig) and sig>0):
                untrade+=notional; continue
            deployed += notional
            kp=strike_delta(float(s0),t,avg_rf,float(sig),cfg['put_delta'],'put')
            pp=bs_price(float(s0),kp,t,avg_rf,float(sig),'put')
            pcash=notional/kp*pp
            put_prem += pcash
            put_payoff += notional/kp*(-max(kp-float(st),0))
            kc=strike_delta(float(s0),t,avg_rf,float(sig),cfg['call_delta'],'call')
            cp=bs_price(float(s0),kc,t,avg_rf,float(sig),'call')
            ccash=notional/float(s0)*cp  # assume covered shares notional equivalent
            call_prem += ccash
            call_payoff += notional/float(s0)*(-max(float(st)-kc,0))
        interest=(cash_nav+untrade)*avg_rf*days/365
        # NAV path follows put-only engine from earlier, to keep annual capital base comparable.
        nav += put_prem + put_payoff + interest
        rows.append({'date':expiry,'year':expiry.year,'nav':nav,'deployed':deployed,'put_premium':put_prem,'call_premium_hyp':call_prem,'put_net':put_prem+put_payoff,'call_net_hyp':call_prem+call_payoff,'cash_interest':interest})
        i+=dte
    df=pd.DataFrame(rows)
    ann=df.groupby('year').agg(ending_nav=('nav','last'), put_premium=('put_premium','sum'), call_premium_hyp=('call_premium_hyp','sum'), put_net=('put_net','sum'), call_net_hyp=('call_net_hyp','sum'), cash_interest=('cash_interest','sum')).reset_index()
    prev=ann['ending_nav'].shift(1).fillna(INITIAL_NAV)
    ann['put_premium_yield_pct']=ann['put_premium']/prev*100
    ann['call_premium_yield_pct']=ann['call_premium_hyp']/prev*100
    ann['total_gross_premium_yield_pct']=(ann['put_premium']+ann['call_premium_hyp'])/prev*100
    ann['put_net_yield_pct']=ann['put_net']/prev*100
    return df, ann

def main():
    closes,irx=download(); all_ann=[]; summaries={}
    for name,cfg in SCENARIOS.items():
        df,ann=run(closes,irx,cfg); ann.insert(0,'scenario',name); all_ann.append(ann)
        df.to_csv(OUT_DIR/f'premium_cycles_{name}.csv',index=False); ann.to_csv(OUT_DIR/f'premium_annual_{name}.csv',index=False)
        summaries[name]={
          'avg_put_premium_yield_pct':float(ann['put_premium_yield_pct'].mean()),
          'avg_call_premium_yield_pct':float(ann['call_premium_yield_pct'].mean()),
          'avg_total_gross_premium_yield_pct':float(ann['total_gross_premium_yield_pct'].mean()),
          'avg_put_net_yield_pct':float(ann['put_net_yield_pct'].mean()),
        }
    comp=pd.concat(all_ann); comp.to_csv(OUT_DIR/'premium_annual_all_scenarios.csv',index=False)
    (OUT_DIR/'premium_summary_all_scenarios.json').write_text(json.dumps(summaries,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summaries,ensure_ascii=False,indent=2))
    cols=['scenario','year','put_premium_yield_pct','call_premium_yield_pct','total_gross_premium_yield_pct','put_net_yield_pct','put_premium','call_premium_hyp']
    print(comp[cols].to_string(index=False, formatters={c:'{:.2f}'.format for c in cols if c.endswith('pct')} | {'put_premium':'{:,.0f}'.format,'call_premium_hyp':'{:,.0f}'.format}))
if __name__=='__main__': main()
