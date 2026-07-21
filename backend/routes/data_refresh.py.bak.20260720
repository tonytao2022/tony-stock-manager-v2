#!/usr/bin/env python3
"""数据刷新API — 板块/技术指标/基本面一键拉取"""
import sys, os, json, math, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from flask import Blueprint, jsonify, request
from collections import defaultdict
import pymysql

router = Blueprint('data_refresh', __name__)

def _get_pwd():
    return re.search(r'password\s*=\s*(\S+)', open('/etc/mysql/debian.cnf').read()).group(1)

def _conn():
    return pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint',
                           password=_get_pwd(), database="stock_db_v2", charset='utf8mb4')

def ok(data):
    return jsonify({"code": 0, "data": data, "message": "success"})
def err(msg):
    return jsonify({"code": -1, "data": None, "message": msg})

# ── 1. 刷新板块数据 ──
@router.route('/api/v2/data/refresh/sector', methods=['POST'])
def refresh_sector():
    try:
        c = _conn(); cu = c.cursor()
        cu.execute("SELECT MAX(trade_date) FROM daily_kline WHERE trade_date <= CURDATE()")
        td = cu.fetchone()[0]
        
        cu.execute("SELECT ts_code, change_pct, vol, amount FROM daily_kline WHERE trade_date=%s", (td,))
        kline_map = {}
        for r in cu.fetchall():
            kline_map[r[0]] = {"p": float(r[1] or 0), "v": float(r[2] or 0), "a": float(r[3] or 0)}
        cu.execute("SELECT ts_code, industry FROM stock_basic WHERE industry IS NOT NULL AND industry != ''")
        ind_map = {}
        for r in cu.fetchall():
            ind_map[r[0]] = (r[1] or '').strip()[:30]
        cu.close()
        sd = defaultdict(lambda: {'n':0,'p':0,'v':0,'a':0})
        for code, ind in ind_map.items():
            if code in kline_map:
                kl = kline_map[code]
                sd[ind]['n'] += 1; sd[ind]['p'] += kl['p']; sd[ind]['v'] += kl['v']; sd[ind]['a'] += kl['a']
        
        cu2 = c.cursor(); ins = 0
        for ind, d in sd.items():
            if d['n'] < 3: continue
            cu2.execute("INSERT INTO sector_index_daily (sector_code,trade_date,open,close,high,low,change_pct,vol,amount) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE change_pct=VALUES(change_pct)", (ind, td, 100, 100, 100, 100, round(d['p']/d['n'],2), round(d['v']/d['n'],2), round(d['a']/d['n'],2)))
            ins += 1
        c.commit(); cu2.close(); c.close()
        return ok({"sectors": ins, "trade_date": str(td)})
    except Exception as e:
        return err(str(e))

# ── 2. 刷新技术指标 ──
@router.route('/api/v2/data/refresh/tech', methods=['POST'])
def refresh_tech():
    try:
        from collections import defaultdict as _dd
        c = _conn(); cu = c.cursor()
        cu.execute("SELECT MAX(trade_date) FROM daily_kline WHERE trade_date <= CURDATE()")
        td = cu.fetchone()[0]
        
        cu.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
        pool = set(r[0] for r in cu.fetchall())
        
        # 技术指标计算需要回看259个交易日（用于250日均线+RSI）
        cu.execute("SELECT DATE_SUB(%s, INTERVAL 259 DAY) as start_date", (td,))
        start_date = cu.fetchone()[0] or '2025-06-10'
        cu.execute("SELECT d.ts_code, d.close, d.vol FROM daily_kline d WHERE d.trade_date >= %s AND d.trade_date <= %s ORDER BY d.ts_code, d.trade_date", (str(start_date), td))
        all_data = _dd(list)
        for r in cu.fetchall():
            if r[0] in pool:
                all_data[r[0]].append({'c': float(r[1] or 0), 'v': float(r[2] or 0)})
        cu.close()
        
        def _sma(v, n):
            if len(v) < n: return sum(v)/len(v) if v else 0
            return sum(v[-n:])/n
        def _rsi(v, n=14):
            if len(v) < n+1: return 50
            g = sum(v[i]-v[i-1] for i in range(-n,0) if v[i]>v[i-1])
            l = sum(v[i-1]-v[i] for i in range(-n,0) if v[i]<=v[i-1])
            ag = g/n; al = l/n
            if al == 0: return 100
            return 100 - 100/(1+ag/al)
        
        cu2 = c.cursor(); done = 0
        for code, klines in all_data.items():
            cl = [k['c'] for k in klines]; vl = [k['v'] for k in klines]
            if len(cl) < 20: continue
            try:
                cu2.execute("INSERT INTO technical_indicator(ts_code,trade_date,ma_5,ma_10,ma_20,ma_60,ma_120,ma_250,rsi_12,rsi_14,volume_ratio)VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)ON DUPLICATE KEY UPDATE ma_5=VALUES(ma_5),rsi_14=VALUES(rsi_14),volume_ratio=VALUES(volume_ratio)", (code,str(td),_sma(cl,5),_sma(cl,10),_sma(cl,20),_sma(cl,60),_sma(cl,120),_sma(cl,250),_rsi(cl,14),_rsi(cl,14),_sma(vl,5) if vl else 1))
                done += 1
            except: pass
        c.commit(); cu2.close(); c.close()
        return ok({"done": done, "total": len(pool), "trade_date": str(td)})
    except Exception as e:
        return err(str(e))

@router.route('/api/v2/data/refresh/basic', methods=['POST'])
def refresh_basic():
    try:
        c = _conn(); cu = c.cursor()
        cu.execute("SELECT MAX(trade_date) FROM daily_kline WHERE trade_date <= CURDATE()")
        td = cu.fetchone()[0]
        ts_date = str(td).replace('-','')
        cu.close()
        
        _pwd = _get_pwd()
        _tc = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint', password=_get_pwd(), database="openclaw_config")
        _tcu = _tc.cursor()
        _tcu.execute("SELECT api_key FROM api_credentials WHERE id=1")
        token = _tcu.fetchone()[0]
        _tcu.close(); _tc.close()
        
        import tushare as ts
        ts.set_token(token)
        pro = ts.pro_api()
        
        c2 = _conn(); cu2 = c2.cursor()
        cu2.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
        codes = set(r[0] for r in cu2.fetchall())
        cu2.close()
        
        # 全市场拉取（1次调用）
        df = pro.daily_basic(trade_date=ts_date)
        if df is None or len(df) == 0:
            return err("未获取到daily_basic数据")
        
        pool_df = df[df['ts_code'].isin(codes)]
        
        cu3 = c2.cursor(); ok_cnt = 0
        for _, r in pool_df.iterrows():
            def sf(v):
                try: v = float(v or 0)
                except: v = 0
                return v if not (math.isnan(v) or math.isinf(v)) else 0
            try:
                cu3.execute("""
                    INSERT INTO daily_basic (ts_code, trade_date, turnover_rate, turnover_rate_f, pe, pe_ttm, pb, ps, ps_ttm, dv_ratio, dv_ttm, total_mv, circ_mv)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE pe=VALUES(pe), pe_ttm=VALUES(pe_ttm), pb=VALUES(pb), total_mv=VALUES(total_mv), circ_mv=VALUES(circ_mv)
                """, (r['ts_code'], str(td), sf(r.get('turnover_rate')), sf(r.get('turnover_rate_f')),
                      sf(r.get('pe')), sf(r.get('pe_ttm')), sf(r.get('pb')), sf(r.get('ps')), sf(r.get('ps_ttm')),
                      sf(r.get('dv_ratio')), sf(r.get('dv_ttm')), sf(r.get('total_mv')), sf(r.get('circ_mv'))))
                ok_cnt += 1
            except: pass
        c2.commit(); cu3.close(); c2.close()
        return ok({"total": ok_cnt, "trade_date": str(td)})
    except Exception as e:
        return err(str(e))
