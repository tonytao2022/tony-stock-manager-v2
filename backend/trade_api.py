#!/usr/bin/env python3
"""
交易记录API服务 (独立FastAPI)
提供: 添加交易, 查询持仓, 查询历史, 加权计算
"""
import sys, os, json, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from datetime import date
import uvicorn

from trade_manager import init_trade_table, add_trade, calc_weighted_avg_hold_days, sync_to_portfolio, get_trade_history

app = FastAPI(title="交易记录API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class TradeIn(BaseModel):
    ts_code: str
    name: str = ''
    trade_date: str = None
    direction: str = 'BUY'
    qty: int
    price: float
    amount: float = 0
    commission: float = 0
    notes: str = ''

@app.on_event("startup")
def startup():
    init_trade_table()

@app.post("/api/trade/add-trade")
async def add_trade_api(t: TradeIn):
    td = t.trade_date or str(date.today())
    try:
        tid = add_trade(t.ts_code, t.name, td, t.direction, t.qty, t.price, t.commission, t.notes)
        sync = sync_to_portfolio(t.ts_code)
        return {
            "code": 0, "status": "ok", "trade_id": tid,
            "sync": sync
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/trade/history")
async def get_history(code: Optional[str] = None, limit: int = 100):
    try:
        tds = get_trade_history(code, limit)
        return {"code": 0, "data": tds, "trades": tds}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/trade/calc/{ts_code}")
async def calc(ts_code: str):
    try:
        r = calc_weighted_avg_hold_days(ts_code)
        return {"code": 0, "data": r}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/trade/portfolio")
async def get_portfolio():
    """获取当前所有持仓的加权数据"""
    try:
        from step_strategy_engine import get_conn
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT ts_code FROM portfolio_holdings WHERE status='HOLDING'")
        codes = [r[0] for r in cur.fetchall()]
        cur.close(); conn.close()
        
        results = []
        for code in codes:
            r = calc_weighted_avg_hold_days(code)
            if r['current_qty'] > 0:
                sr = sync_to_portfolio(code)
                if sr: results.append(sr)
        return {"code": 0, "data": results, "holdings": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/trade/health")
async def health():
    return {"status": "ok", "service": "trade-manager"}

if __name__ == '__main__':
    uvicorn.run(app, host="0.0.0.0", port=8892)
