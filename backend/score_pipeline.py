#!/usr/bin/env python3
"""
P6双轨评分管道 — 独立脚本，供cron调用
直接从数据库读已拉取的数据，执行全量评分并入库
"""
import pymysql, sys
sys.path.insert(0, '.')
from p6_dual_track_engine import batch_score, MarketContext
from season_engine import SeasonEngine

DB = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint',
      'password':'iXve1rVBXfdA4tL9','database':'stock_db_v2',
      'charset':'utf8mb4','connect_timeout':10,'read_timeout':300,'write_timeout':300,
      'autocommit':True,'cursorclass':pymysql.cursors.DictCursor}

# 1. 季节判定 + 入库
engine = SeasonEngine()
judge_result = engine.judge_market_season()
ctx = MarketContext(judge_result)
print('📊 市场: %s/%s | 评分日期: %s' % (ctx.season, ctx.regime, ctx.trade_date))

# 1b. 季节入库（补坑：管道从不写season_state）
try:
    from season_engine import save_result_to_db
    save_result_to_db(judge_result)
    print('  ✅ 季节判定已入库')
except Exception as e:
    print('  ⚠️ 季节入库失败: %s' % str(e)[:60])

# 1c. 缠论分析入库 — 逐只分析+写库
try:
    from engine.chanlun_batch import analyze_pool_for_date
    analyze_pool_for_date(str(ctx.trade_date))
    print('  ✅ 缠论分析已入库')
except Exception as e:
    print('  ⚠️ 缠论分析失败: %s (降级跳过)' % str(e)[:80])

# 2. 评分池
conn = pymysql.connect(**DB)
cur = conn.cursor()
cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
ts_codes = [row['ts_code'] for row in cur.fetchall()]
cur.close()
conn.close()
print('📈 评分池: %d只' % len(ts_codes))

# 3. 评分
results = batch_score(ts_codes, ctx)
print('🔒 评分完成: %d只' % len(results))

# 4. 入库
conn2 = pymysql.connect(**DB)
cur2 = conn2.cursor()
td = str(ctx.trade_date)

saved, skipped = 0, 0
# 构建个股→指数季节映射表
season_conn = pymysql.connect(**DB)
season_cur = season_conn.cursor()
season_cur.execute("SELECT index_code, season FROM season_state WHERE trade_date=%s", (str(ctx.trade_date),))
season_rows = season_cur.fetchall()
season_cur.close(); season_conn.close()
index_season_map = {r['index_code']: r['season'] for r in season_rows}

def get_stock_season(ts_code: str) -> str:
    """根据股票代码前缀映射到对应指数的季节"""
    if ts_code.endswith('.SH'):
        if ts_code.startswith('688'):
            return index_season_map.get('000688.SH', ctx.season)  # 科创板→科创50
        else:
            return index_season_map.get('000001.SH', ctx.season)  # 上证→上证指数
    elif ts_code.endswith('.SZ'):
        if ts_code.startswith('300'):
            return index_season_map.get('399006.SZ', ctx.season)  # 创业板→创业板指
        else:
            return index_season_map.get('399001.SZ', ctx.season)  # 深证/中小→深成指
    return ctx.season

for i, r in enumerate(results):
    try:
        code = r['ts_code']
        stock_season = get_stock_season(code)
        ding = r.get('details', {}) or {}
        calib = float(r.get('calibrated_score',0))
        op_mode = 'attack' if calib >= 75 else ('normal' if calib >= 60 else ('defense' if calib >= 40 else 'dormant'))
        sig_conf = 'high' if calib >= 80 else ('medium' if calib >= 60 else 'low')
        track_type = r.get('track', '')
        if track_type in ('momentum', 'momentum_fallback'):
            tr_score = float(ding.get('chanlun_trend',0) or 0)    # 引擎输出chanlun_trend
            ss_score = float(ding.get('structure_score',0) or 0)  # 缠论结构分
            mo_score = float(ding.get('momentum_raw',0) or 0)
            po_score = float(ding.get('pos_score',0) or 0)
            mf_v = float(ding.get('mf_score',0) or 0)
            mg_score = float(ding.get('margin_score',0) or 0)
            vr = float(ding.get('vol_ratio',1.0) or 1.0)
        else:
            tr_score = float(ding.get('structure_factor',0) or 0)
            ss_score = float(ding.get('structure_score',0) or 0)  # B轨结构分
            mo_score = float(ding.get('oversold_factor',0) or 0)
            po_score = float(ding.get('pos_score',0) or 0)
            mf_v = float(ding.get('mf_score',0) or 0)
            mg_score = float(ding.get('margin_score',0) or 0)
            vr = 1.0
        
        # V12.5: 短期信号分
        stf = r.get('stf', {}) or {}
        stf_score = float(stf.get('short_term_score', 50) or 50)
        stf_capital = float(stf.get('capital_inertia', 50) or 50)
        stf_volume = float(stf.get('volume_health', 50) or 50)
        stf_overbought = float(stf.get('overbought_safety', 50) or 50)
        stf_momentum = float(stf.get('short_momentum', 50) or 50)
        
        cur2.execute("""
            INSERT INTO strategy_signal 
                (ts_code, trade_date, track, composite_score, calibrated_score,
                 scoring_strategy, direction, operation_mode, buy_sell_point,
                 reason_chain, signal_confidence, autumn_tiger, tiger_confidence,
                 hengjiyuan_level, trend_score, structure_score, momentum_score, pos_score, mf_score, margin_score, vol_ratio,
                 season, short_term_score, stf_capital, stf_volume, stf_overbought, stf_momentum)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                composite_score=VALUES(composite_score), calibrated_score=VALUES(calibrated_score),
                trend_score=VALUES(trend_score), structure_score=VALUES(structure_score),
                momentum_score=VALUES(momentum_score),
                pos_score=VALUES(pos_score), mf_score=VALUES(mf_score),
                margin_score=VALUES(margin_score), vol_ratio=VALUES(vol_ratio),
                season=VALUES(season),
                short_term_score=VALUES(short_term_score),
                stf_capital=VALUES(stf_capital), stf_volume=VALUES(stf_volume),
                stf_overbought=VALUES(stf_overbought), stf_momentum=VALUES(stf_momentum)
        """, (code, td, r['track'], float(r['score']), float(r.get('calibrated_score',0)),
              'dual_track_v1', op_mode, '', '', '', sig_conf, 0, 0.0, 'weak_heng',
              tr_score, ss_score, mo_score, po_score, mf_v, mg_score, vr,
              stock_season, stf_score, stf_capital, stf_volume, stf_overbought, stf_momentum))
        saved += 1
    except Exception as e:
        skipped += 1
        if skipped <= 3: print('  ⚠️ %s: %s' % (r['ts_code'], str(e)[:60]))

cur2.close()
conn2.close()
print('📦 入库: %d | 跳过: %d' % (saved, skipped))

# TOP10展示
conn3 = pymysql.connect(**DB)
cur3 = conn3.cursor()
cur3.execute("""
    SELECT ss.ts_code, sb.name, ss.calibrated_score, ss.composite_score
    FROM strategy_signal ss
    LEFT JOIN stock_basic sb ON ss.ts_code=sb.ts_code
    WHERE ss.trade_date=%s AND ss.gate_triggered=0
    ORDER BY ss.calibrated_score DESC LIMIT 10
""", (td,))
print('🏆 TOP 10 (%s)' % td)
for i, r in enumerate(cur3.fetchall()):
    sc = float(r.get('calibrated_score',0) or 0)
    cs = float(r.get('composite_score',0) or 0)
    print('  %2d. %-10s %-8s 校准:%5.1f 原始:%5.1f' % (i+1, r['ts_code'], r.get('name',''), sc, cs))
cur3.close()
conn3.close()
