#!/usr/bin/env python3
"""
收盘数据拉取管道 — 独立脚本，供cron调用
拉取: daily_kline + daily_basic + moneyflow + 指数K线
只拉监控池股票（watch_pool），不拉全市场
"""
import sys, tushare as ts
from datetime import datetime
from db_config import get_connection, get_tushare_token

ts.set_token(get_tushare_token())
pro = ts.pro_api()
conn = get_connection()

today = datetime.now().strftime('%Y%m%d')
today_fmt = datetime.now().strftime('%Y-%m-%d')

def log(msg):
    print(f"  {msg}", flush=True)

def get_pool_codes(cursor) -> list:
    """获取监控池活跃股票代码列表"""
    cursor.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
    return [r['ts_code'] for r in cursor.fetchall()]

def batch_query(api_func, codes, **kwargs):
    """分批查询，每批100只（Tushare支持逗号分隔）"""
    all_rows = []
    for i in range(0, len(codes), 100):
        batch = codes[i:i+100]
        codes_str = ','.join(batch)
        try:
            df = api_func(ts_code=codes_str, **kwargs)
            if df is not None and not df.empty:
                all_rows.extend(df.to_dict('records'))
        except Exception as e:
            log(f"  分批{i}失败: {str(e)[:50]}")
    return all_rows

c = conn.cursor()
pool_codes = get_pool_codes(c)
log(f"监控池共{len(pool_codes)}只股票")

# ─── Step 1: 监控池日K线 ───
print("[1/5] daily 监控池K线...", flush=True)

rows = batch_query(pro.daily, pool_codes, trade_date=today)
log(f"拉取 {len(rows)}条")

saved = 0
for r in rows:
    try:
        c.execute("""INSERT INTO daily_kline (ts_code,trade_date,open,high,low,close,pre_close,change_pct,vol,amount)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE open=VALUES(open),high=VALUES(high),low=VALUES(low),
            close=VALUES(close),pre_close=VALUES(pre_close),change_pct=VALUES(change_pct),
            vol=VALUES(vol),amount=VALUES(amount)""",
            (r['ts_code'],today_fmt,float(r.get('open',0)or 0),float(r.get('high',0)or 0),
             float(r.get('low',0)or 0),float(r.get('close',0)or 0),
             float(r.get('pre_close',0)or 0),float(r.get('pct_chg',0)or 0),
             float(r.get('vol',0)or 0),float(r.get('amount',0)or 0)))
        saved += 1
    except: pass
log(f"✅ K线入库 {saved}条")

# ─── Step 1.5: daily_basic 基本面指标（监控池） ───
print("[1.5/5] daily_basic (PE/PB/换手率/市值)...", flush=True)

rows_basic = batch_query(pro.daily_basic, pool_codes, trade_date=today)
log(f"拉取 {len(rows_basic)}条")

saved_basic = 0
for r in rows_basic:
    try:
        c.execute("""INSERT INTO daily_basic
            (ts_code, trade_date, turnover_rate, turnover_rate_f,
             pe, pe_ttm, pb, ps, ps_ttm, dv_ratio, dv_ttm,
             total_mv, circ_mv)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
             turnover_rate=VALUES(turnover_rate),
             turnover_rate_f=VALUES(turnover_rate_f),
             pe=VALUES(pe), pe_ttm=VALUES(pe_ttm), pb=VALUES(pb),
             ps=VALUES(ps), ps_ttm=VALUES(ps_ttm),
             dv_ratio=VALUES(dv_ratio), dv_ttm=VALUES(dv_ttm),
             total_mv=VALUES(total_mv), circ_mv=VALUES(circ_mv)""",
            (r['ts_code'], today_fmt,
             float(r.get('turnover_rate',0)) if r.get('turnover_rate') else None,
             float(r.get('turnover_rate_f',0)) if r.get('turnover_rate_f') else None,
             float(r.get('pe',0)) if r.get('pe') else None,
             float(r.get('pe_ttm',0)) if r.get('pe_ttm') else None,
             float(r.get('pb',0)) if r.get('pb') else None,
             float(r.get('ps',0)) if r.get('ps') else None,
             float(r.get('ps_ttm',0)) if r.get('ps_ttm') else None,
             float(r.get('dv_ratio',0)) if r.get('dv_ratio') else None,
             float(r.get('dv_ttm',0)) if r.get('dv_ttm') else None,
             float(r.get('total_mv',0)) if r.get('total_mv') else None,
             float(r.get('circ_mv',0)) if r.get('circ_mv') else None))
        saved_basic += 1
    except Exception as e:
        pass
log(f"✅ daily_basic入库 {saved_basic}条")

# ─── Step 2: moneyflow（监控池） ───
print("[2/5] moneyflow...", flush=True)
rows2 = batch_query(pro.moneyflow, pool_codes, trade_date=today)
log(f"拉取 {len(rows2)}条")
saved2 = 0
for r in rows2:
    try:
        c.execute("""INSERT INTO moneyflow (ts_code,trade_date,net_mf_amount,buy_lg_amount,sell_lg_amount,buy_sm_amount,sell_sm_amount)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE net_mf_amount=VALUES(net_mf_amount),
            buy_lg_amount=VALUES(buy_lg_amount),sell_lg_amount=VALUES(sell_lg_amount)""",
            (r['ts_code'],today_fmt,float(r.get('net_mf_amount',0)or 0),
             float(r.get('buy_lg_amount',0)or 0),float(r.get('sell_lg_amount',0)or 0),
             float(r.get('buy_sm_amount',0)or 0),float(r.get('sell_sm_amount',0)or 0)))
        saved2 += 1
    except: pass
log(f"✅ moneyflow入库 {saved2}条")

# ─── Step 3: 指数日线 ───
print("[3/5] 指数日线...", flush=True)
index_codes = ['000001.SH','000300.SH','000688.SH','399001.SZ','399006.SZ','399106.SZ']
for code in index_codes:
    try:
        dfi = pro.daily(ts_code=code, start_date=today, end_date=today)
        if dfi is not None and len(dfi) > 0:
            r = dfi.iloc[0]
            c.execute("""INSERT INTO daily_kline (ts_code,trade_date,open,high,low,close,pre_close,change_pct,vol,amount)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE open=VALUES(open),close=VALUES(close),change_pct=VALUES(change_pct)""",
                (r['ts_code'],today_fmt,float(r.get('open',0)or 0),float(r.get('high',0)or 0),
                 float(r.get('low',0)or 0),float(r.get('close',0)or 0),
                 float(r.get('pre_close',0)or 0),float(r.get('pct_chg',0)or 0),
                 float(r.get('vol',0)or 0),float(r.get('amount',0)or 0)))
            log(f"  指数 {code} {r.get('close',0)} 已入库")
    except Exception as e:
        log(f"  指数 {code} 失败: {e}")

# ─── Step 4: 融资融券 —— 按监控池股票循环拉取（margin_detail接口限制单只查） ───
print("[4/5] margin_detail 融资融券...", flush=True)
try:
    c = conn.cursor()
    c.execute('SELECT MAX(trade_date) FROM margin_detail')
    last_margin_dt = str(c.fetchone()['MAX(trade_date)'] or '')
    target_dt = today_fmt
    
    # Tushare margin_detail接口T+1~T+2更新，如果今天的没数据就用前一天的
    if last_margin_dt >= target_dt:
        log(f"{target_dt} 已是最新, 跳过")
    else:
        # 从T-1天开始尝试
        from datetime import timedelta
        days_to_check = [1, 2, 3]
        margin_df = None
        margin_dt = None
        for d in days_to_check:
            check_dt = (datetime.now() - timedelta(days=d))
            dt_str = check_dt.strftime('%Y%m%d')
            dt_fmt = check_dt.strftime('%Y-%m-%d')
            if last_margin_dt >= dt_fmt:
                break
            df_tmp = pro.margin_detail(trade_date=dt_str)
            if df_tmp is not None and len(df_tmp) > 0:
                margin_df = df_tmp
                margin_dt = dt_fmt
                log(f"拉取 {dt_fmt}: {len(margin_df)}条")
                break
        
        if margin_df is not None and len(margin_df) > 0:
            saved = 0
            for _, r in margin_df.iterrows():
                try:
                    c.execute('''INSERT IGNORE INTO margin_detail 
                        (ts_code, trade_date, rzye, rqye, rzmre, rqyl, rzche, rqchl, rqmcl, rzrqye)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                        (r['ts_code'], r['trade_date'],
                         float(r['rzye']) if r.get('rzye') else None,
                         float(r['rqye']) if r.get('rqye') else None,
                         float(r['rzmre']) if r.get('rzmre') else None,
                         float(r['rqyl']) if r.get('rqyl') else None,
                         float(r['rzche']) if r.get('rzche') else None,
                         float(r['rqchl']) if r.get('rqchl') else None,
                         float(r['rqmcl']) if r.get('rqmcl') else None,
                         float(r['rzrqye']) if r.get('rzrqye') else None))
                    saved += 1
                except: pass
            log(f"✅ margin_detail入库 {saved}条 ({margin_dt})")
        else:
            log("margin_detail: 最近3天无新数据（T+1后自动补入）")
    c.close()
except Exception as e:
    log(f"margin_detail: {e}")

# ─── Step 5: daily_kline_qfq 前复权K线同步 - 从daily_kline复制（Tushare无批量复权接口） ───
# pro.daily()返回不复权数据，freshness_checker.py也是直接pro.daily()→qfq表
# 最可靠的方案：收盘后直接用daily_kline当天的全量数据复制到daily_kline_qfq
print("[5/5] daily_kline_qfq 日K线(前复权)...", flush=True)
try:
    c5 = conn.cursor()
    c5.execute('SELECT MAX(trade_date) FROM daily_kline_qfq')
    last_qfq = str(c5.fetchone()['MAX(trade_date)'] or '')
    if last_qfq >= today_fmt:
        log(f"{today_fmt} 已是最新, 跳过")
    else:
        # 从daily_kline复制当天数据到daily_kline_qfq
        c5.execute('''SELECT ts_code, trade_date, close, pre_close, change_pct, vol, amount 
                     FROM daily_kline WHERE trade_date=%s''', (today_fmt,))
        rows5 = c5.fetchall()
        saved5 = 0
        for r in rows5:
            try:
                c5.execute('''INSERT IGNORE INTO daily_kline_qfq 
                    (ts_code, trade_date, close, pre_close, change_pct, vol, amount)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                    (r['ts_code'], r['trade_date'], float(r['close'] or 0),
                     float(r['pre_close'] or 0), float(r['change_pct'] or 0),
                     int(r['vol'] or 0), float(r['amount'] or 0)))
                saved5 += 1
            except: pass
        log(f"✅ daily_kline_qfq入库 {saved5}条")
    c5.close()
except Exception as e:
    log(f"daily_kline_qfq: {e}")

conn.close()
print("🏁 数据拉取完成", flush=True)
