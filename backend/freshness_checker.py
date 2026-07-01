#!/usr/bin/env python3
"""
数据保鲜检查 — V2版
检查stock_db_v2各表数据是否滞后，自动补拉缺失数据
"""
import tushare as ts, pymysql, sys
from datetime import datetime, date

ts.set_token('d2b88da51a08626fd23b7be11418c593ccdee21a94d2e2aef4a334ad')
pro = ts.pro_api()

MYSQL_PASS = 'iXve1rVBXfdA4tL9'
today = datetime.now().strftime('%Y-%m-%d')
today_int = datetime.now().strftime('%Y%m%d')

DB = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint',
      'password':MYSQL_PASS,'database':'stock_db_v2',
      'charset':'utf8mb4','autocommit':True,'cursorclass':pymysql.cursors.DictCursor}

checks = [
    ('daily_kline_qfq', '日K线'),
    ('strategy_signal', '策略评分'),
    ('moneyflow', '资金流向'),
    ('season_state', '季节判定'),
]

print('🔍 V2数据保鲜检查 - %s' % datetime.now().strftime('%H:%M'))
print('='*50)

conn = pymysql.connect(**DB)
cur = conn.cursor()
need_fix = False

for tbl, label in checks:
    cur.execute("SELECT MAX(trade_date) FROM %s" % tbl)
    max_date = cur.fetchone()['MAX(trade_date)']
    status = '✅' if max_date and str(max_date) >= today else '⚠️'
    print('  %s %s: 最新=%s' % (status, label, max_date or '无数据'))
    if not max_date or str(max_date) < today:
        need_fix = True

if need_fix:
    print('\n⚠️ 发现数据滞后，自动补拉...')
    
    # 补拉K线
    cur.execute("SELECT MAX(trade_date) FROM daily_kline_qfq")
    last = str(cur.fetchone()['MAX(trade_date)'] or '')[:10]
    if last < today:
        print('  补拉K线 %s→%s...' % (last, today))
        df = pro.query('daily', trade_date=today_int,
                       fields='ts_code,trade_date,close,pre_close,pct_chg,vol,amount')
        saved = 0
        for _, r in df.iterrows():
            try:
                cur.execute("""
                    INSERT INTO daily_kline_qfq (ts_code,trade_date,close,pre_close,change_pct,vol,amount)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE close=VALUES(close),change_pct=VALUES(change_pct)
                """, (r['ts_code'],today,float(r.get('close',0)),float(r.get('pre_close',0)),
                      float(r.get('pct_chg',0)),int(r.get('vol',0)or 0),float(r.get('amount',0)or 0)))
                saved += 1
            except: pass
        print('    ✅ K线入库 %d条' % saved)
    
    # 补拉资金流向
    cur.execute("SELECT MAX(trade_date) FROM moneyflow")
    last_mf = str(cur.fetchone()['MAX(trade_date)'] or '')[:10]
    if last_mf < today:
        print('  补拉资金流向...')
        df2 = pro.query('moneyflow', trade_date=today_int,
                        fields='ts_code,trade_date,net_mf_amount,buy_lg_amount,sell_lg_amount')
        saved = 0
        for _, r in df2.iterrows():
            try:
                cur.execute("""
                    INSERT INTO moneyflow (ts_code,trade_date,net_mf_amount,buy_lg_amount,sell_lg_amount)
                    VALUES (%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE net_mf_amount=VALUES(net_mf_amount)
                """, (r['ts_code'],today_int,float(r.get('net_mf_amount',0)),
                      float(r.get('buy_lg_amount',0)),float(r.get('sell_lg_amount',0))))
                saved += 1
            except: pass
        print('    ✅ 资金流向入库 %d条' % saved)
    
    # 补拉评分
    cur.execute("SELECT MAX(trade_date) FROM strategy_signal")
    last_ss = str(cur.fetchone()['MAX(trade_date)'] or '')[:10]
    if last_ss < today:
        print('  补拉评分...')
        sys.path.insert(0, '/root/stock-system-v2/backend')
        from season_engine import SeasonEngine
        engine = SeasonEngine()
        judge_result = engine.judge_market_season()
        
        from p6_dual_track_engine import MarketContext, batch_score
        ctx = MarketContext(judge_result)
        
        cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
        codes = [r['ts_code'] for r in cur.fetchall()]
        results = batch_score(codes, ctx)
        
        saved = 0
        for r in results:
            try:
                ding = r.get('details', {})
                cur.execute("""
                    INSERT INTO strategy_signal (ts_code,trade_date,track,composite_score,calibrated_score,
                        scoring_strategy,operation_mode,signal_confidence,hengjiyuan_level,
                        trend_score,momentum_score,pos_score,mf_score,margin_score,vol_ratio)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE composite_score=VALUES(composite_score),calibrated_score=VALUES(calibrated_score)
                """, (r['ts_code'],today,r['track'],float(r['score']),float(r.get('calibrated_score',0)),
                      'dual_track_v1','','','weak_heng',
                      float(ding.get('trend_score',0)),float(ding.get('momentum_raw',0)),
                      float(ding.get('pos_score',0)),float(ding.get('mf_score',0)),
                      float(ding.get('margin_score',0)),float(ding.get('vol_ratio',0))))
                saved += 1
            except: pass
        print('    ✅ 评分入库 %d只' % saved)

cur.close()
conn.close()
print('🏁 保鲜检查完成')
