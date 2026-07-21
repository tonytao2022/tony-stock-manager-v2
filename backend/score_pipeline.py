#!/usr/bin/env python3
"""
P6双轨评分管道 — 独立脚本，供cron调用
直接从数据库读已拉取的数据，执行全量评分并入库
"""
import pymysql, sys, os
# 确保能找到同级模块和engine目录
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
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
        # 惩罚分（V13.3新增）
        p_score = float(ding.get('penalty_score', 0) or 0)
        p_reason = ding.get('penalty_reason', '') or ''
        # 引擎计算的修后评分（已在score_stock中减去了penalty）
        adjusted_score = float(r['score'])

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
                 season, penalty_score, penalty_reason,
                 short_term_score, stf_capital, stf_volume, stf_overbought, stf_momentum)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                composite_score=VALUES(composite_score), calibrated_score=VALUES(calibrated_score),
                trend_score=VALUES(trend_score), structure_score=VALUES(structure_score),
                momentum_score=VALUES(momentum_score),
                pos_score=VALUES(pos_score), mf_score=VALUES(mf_score),
                margin_score=VALUES(margin_score), vol_ratio=VALUES(vol_ratio),
                season=VALUES(season),
                penalty_score=VALUES(penalty_score),
                penalty_reason=VALUES(penalty_reason),
                short_term_score=VALUES(short_term_score),
                stf_capital=VALUES(stf_capital), stf_volume=VALUES(stf_volume),
                stf_overbought=VALUES(stf_overbought), stf_momentum=VALUES(stf_momentum)
        """, (code, td, r['track'], adjusted_score, float(r.get('calibrated_score',0)),
              'dual_track_v1', op_mode, '', '', '', sig_conf, 0, 0.0, 'weak_heng',
              tr_score, ss_score, mo_score, po_score, mf_v, mg_score, vr,
              stock_season, p_score, p_reason,
              stf_score, stf_capital, stf_volume, stf_overbought, stf_momentum))
        
        # 同步写入daily_score_snapshot（前端v2-scores.html使用的评分快照表）
        try:
            cur2.execute("""
                INSERT INTO daily_score_snapshot 
                (trade_date, ts_code, name, calibrated_score, composite_score,
                 close_price, change_pct, season, signal_label)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    calibrated_score=VALUES(calibrated_score),
                    composite_score=VALUES(composite_score),
                    close_price=VALUES(close_price),
                    change_pct=VALUES(change_pct),
                    season=VALUES(season),
                    signal_label=VALUES(signal_label)
            """, (
                td, code, r.get('name',''),
                float(r.get('calibrated_score',0)), float(r['score']),
                float(r.get('close_price',0)), float(r.get('change_pct',0)),
                stock_season, r.get('signal_label','')
            ))
        except Exception as e2:
            pass
        
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

# ============================================================
# [V14] H5 Alpha评分层 — 替换L3情绪因子
# 使用正向5因子：alpha005/034/046/062/089
# emotion_score 字段现在存储H5评分（替代原L3情绪因子）
# ============================================================
print('🧬 [V14] H5 Alpha评分层...')
try:
    sys.path.insert(0, '/opt/stock-analyzer')
    from v14_engine import compute_h5_scores
    
    # 趋势季节差异化权重
    # summer/spring: 10% (动量主驱，H5辅助)
    # weak_spring/chaos_spring: 15% 
    # chaos: 20% (评分区分度不足，H5提供增量)
    # chaos_autumn/weak_autumn/autumn: 15~
    # winter: 10% (低仓位，H5意义不大)
    SEASON_H5_WEIGHTS = {
        'summer': 0.10, 'spring': 0.10, 'weak_spring': 0.15, 'chaos_spring': 0.15,
        'chaos': 0.20, 'chaos_autumn': 0.15, 'weak_autumn': 0.15, 'autumn': 0.15,
        'winter': 0.10,
    }
    
    # 获取当日趋势季节
    conn4 = pymysql.connect(**DB)
    cur4 = conn4.cursor()
    cur4.execute("SELECT season FROM season_state WHERE index_code='MARKET' AND trade_date=%s", (td,))
    season_row = cur4.fetchone()
    season = season_row['season'] if season_row else 'chaos'
    h5_weight = SEASON_H5_WEIGHTS.get(season, 0.15)
    short_alpha = h5_weight  # 弹性混合: blended = α×short + (1-α)×mid
    print(f'  🎯 趋势季节: {season} → α(弹性混合)={short_alpha:.0%} (blended = α×H5 + (1-α)×P6)')
    
    # 计算H5评分
    h5_map = compute_h5_scores(td)
    if h5_map and len(h5_map) > 50:
        # 获取该日strategy_signal列表
        cur4.execute("SELECT ts_code FROM strategy_signal WHERE trade_date=%s", (td,))
        signal_codes = [r['ts_code'] for r in cur4.fetchall()]
        
        # 批量更新emotion_score（保持原始H5评分，不混合）
        # 前端/策略读取strategy_signal表的emotion_score时拿到的是H5
        # V14混合评分在daily_v14_score表中以v14_score字段存放
        updated = 0
        v14_map = {}  # {code: v14_score}
        
        # 读取P6评分做V14混合
        cur4.execute("SELECT ts_code, composite_score FROM strategy_signal WHERE trade_date=%s", (td,))
        p6_scores = {r['ts_code']: float(r['composite_score'] or 0) for r in cur4.fetchall()}
        
        for code in signal_codes:
            h5_val = h5_map.get(code)
            if h5_val is not None:
                # 更新emotion_score存储原始H5，同时写入short_term_score
                cur4.execute(
                    "UPDATE strategy_signal SET emotion_score=%s, short_term_score=%s WHERE ts_code=%s AND trade_date=%s",
                    (h5_val, h5_val, code, td))
                updated += 1
                
                # 计算V14混合分
                p6 = p6_scores.get(code, 50)
                v14 = p6 * (1 - h5_weight) + h5_val * h5_weight
                v14_map[code] = round(v14, 1)
        
        conn4.commit()
        
        # 写入daily_v14_score表
        if not v14_map:
            pass
        else:
            # 确保表存在
            cur4.execute("""
                CREATE TABLE IF NOT EXISTS daily_v14_score (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    ts_code VARCHAR(20) NOT NULL,
                    trade_date DATE NOT NULL,
                    v14_score DECIMAL(6,1),
                    p6_score DECIMAL(6,1),
                    h5_score DECIMAL(6,1),
                    h5_weight DECIMAL(4,2),
                    UNIQUE KEY uk_stock_date (ts_code, trade_date)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            written = 0
            for code, v14 in v14_map.items():
                p6 = p6_scores.get(code, 50)
                h5 = h5_map.get(code, 50)
                cur4.execute("""
                    INSERT INTO daily_v14_score (ts_code, trade_date, v14_score, p6_score, h5_score, h5_weight)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE v14_score=VALUES(v14_score),
                        p6_score=VALUES(p6_score), h5_score=VALUES(h5_score)
                """, (code, td, v14, round(p6, 1), h5, h5_weight))
                written += 1
            conn4.commit()
            print(f'  ✅ emotion_score→H5: {updated}只 | daily_v14_score: {written}只 (权重{h5_weight:.0%})')
        
        # 更新daily_score_snapshot表的h5_score和v14_score
        snap_updated = 0
        for code, v14 in v14_map.items():
            h5 = h5_map.get(code, 50)
            cur4.execute(
                "UPDATE daily_score_snapshot SET h5_score=%s, v14_score=%s WHERE ts_code=%s AND trade_date=%s",
                (h5, v14, code, td))
            snap_updated += 1
            if snap_updated % 200 == 0:
                conn4.commit()
        conn4.commit()
        if snap_updated > 0:
            print(f'  ✅ daily_score_snapshot h5/v14更新: {snap_updated}只')
        
        # 写入 bt_s1_score / bt_m1_score 回测表
        bt_s1_written = 0
        for code, v14 in v14_map.items():
            h5 = h5_map.get(code, 50)
            # bt_s1_score: S1评分 = H5评分
            cur4.execute("""
                INSERT INTO bt_s1_score (ts_code, trade_date, s1_score)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE s1_score=VALUES(s1_score)
            """, (code, td, h5))
            bt_s1_written += 1
            if bt_s1_written % 300 == 0: cur4.connection.commit()
        cur4.connection.commit()
        if bt_s1_written > 0:
            print(f'  ✅ bt_s1_score: {bt_s1_written}条')
        
        # bt_m1_score: M1评分 = composite_score
        bt_m1_written = 0
        for code in p6_scores:
            m1 = p6_scores[code]
            cur4.execute("""
                INSERT INTO bt_m1_score (ts_code, trade_date, m1_score)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE m1_score=VALUES(m1_score)
            """, (code, td, round(m1, 1)))
            bt_m1_written += 1
            if bt_m1_written % 300 == 0: cur4.connection.commit()
        cur4.connection.commit()
        if bt_m1_written > 0:
            print(f'  ✅ bt_m1_score: {bt_m1_written}条')
        
        cur4.close()
        conn4.close()
    else:
        cur4.close(); conn4.close()
        print('  ⚠️ H5评分数据不足(%d只)，跳过替换' % (len(h5_map) if h5_map else 0))
except Exception as e:
    print('  ❌ V14集成失败: %s' % str(e)[:100])
    import traceback
    traceback.print_exc()

# ============================================================
# [PATCH V13.3d] 风控降级 + 评分后处理层
# 依赖: risk_downgrade.py / score_post_processor.py
# 必须在评分入库后执行，确保全量评分已有
# ============================================================
try:
    sys.path.insert(0, '/opt/stock-analyzer')
    from risk_downgrade import run_risk_downgrade
    level = run_risk_downgrade(str(ctx.trade_date))
    print('  🔒 [V13.3d] 风控降级: %s' % level)
except Exception as e:
    print('  ⚠️ [V13.3d] 风控降级失败: %s' % str(e)[:80])
    import traceback; traceback.print_exc()

try:
    from score_post_processor import run_batch_from_db
    n = run_batch_from_db(str(ctx.trade_date))
    print('  🧹 [V13.3d] 后处理层: %d条' % n)
except Exception as e:
    print('  ⚠️ [V13.3d] 后处理层失败: %s' % str(e)[:80])
    import traceback; traceback.print_exc()

# ============================================================
# [season同步] 用season_state的个股板块映射补填strategy_signal.season
# ============================================================
try:
    print('🌍 [season] 同步个股板块季节到strategy_signal.season...')
    conn_season = pymysql.connect(**DB)
    cur_season = conn_season.cursor()
    
    # 先写个股板块映射临时表
    cur_season.execute("DROP TABLE IF EXISTS tmp_season_map")
    cur_season.execute("""
        CREATE TABLE tmp_season_map (
            ts_code VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL PRIMARY KEY,
            index_code VARCHAR(20) NOT NULL
        ) ENGINE=MEMORY
    """)
    cur_season.execute("""
        INSERT INTO tmp_season_map (ts_code, index_code)
        SELECT sb.ts_code,
          CASE 
            WHEN RIGHT(sb.ts_code, 3) = '.SH' AND sb.market = '科创板' THEN '000688.SH'
            WHEN RIGHT(sb.ts_code, 3) = '.SH' THEN '000001.SH'
            WHEN RIGHT(sb.ts_code, 3) = '.SZ' AND sb.market = '创业板' THEN '399006.SZ'
            WHEN RIGHT(sb.ts_code, 3) = '.SZ' THEN '399001.SZ'
            ELSE 'MARKET'
          END
        FROM stock_basic sb WHERE sb.is_active = 1
    """)
    
    # 更新season
    cur_season.execute("""
        UPDATE strategy_signal ss
        JOIN tmp_season_map m ON ss.ts_code = m.ts_code
        JOIN season_state sst ON m.index_code = sst.index_code AND ss.trade_date = sst.trade_date
        SET ss.season = sst.season
        WHERE ss.trade_date = %s AND (ss.season IS NULL OR ss.season != sst.season)
    """, (str(ctx.trade_date),))
    updated = cur_season.rowcount
    cur_season.execute("DROP TABLE IF EXISTS tmp_season_map")
    cur_season.close()
    conn_season.close()
    if updated > 0:
        print(f'  ✅ season同步: {updated}只个股季节已更新（个股板块级）')
    else:
        print(f'  ✅ season同步: 无需更新')
except Exception as e:
    print('  ⚠️ [season] 同步失败: %s' % str(e)[:100])

print('📦 [V13.3d] 评分管道完成 ✅')
