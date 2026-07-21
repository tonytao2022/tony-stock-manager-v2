#!/usr/bin/env python3
"""
V13.3e 历史评分回填 —— 用新引擎重跑所有历史评分
用法: python3 backfill_v133e_history.py [start_date] [end_date]

注意: 会覆盖 strategy_signal 表中相应日期的数据
"""
import sys, os, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, '/opt/stock-analyzer')

from datetime import datetime, date, timedelta
from typing import List
from p6_dual_track_engine import (
    SeasonEngine, MarketContext, score_stock, 
    calibrate_scores, _apply_filters
)
from db_config import get_connection


def get_trading_days(start, end):
    """获取交易日列表"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT trade_date FROM daily_kline
        WHERE trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date
    """, (start, end))
    days = [str(r['trade_date']) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return days


def get_watch_pool():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
    codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return codes


def backfill_single_date(td: str, codes: List[str], engine: SeasonEngine):
    """对单个交易日执行V13.3e评分并写入strategy_signal"""
    
    # 1. 用历史日期初始化季节引擎
    # SeasonEngine的judge_history支持指定日期范围
    result = engine.judge_market_season(specific_date=td)
    if not result:
        print(f"  ⚠️ {td} 季节判定失败，跳过")
        return 0, 0
    
    ctx = MarketContext(result)
    # 强制覆盖trade_date
    ctx.trade_date = td
    
    # 2. 批量评分
    results = []
    for code in codes:
        r = score_stock(code, ctx)
        results.append(r)
    
    # 3. 校准
    calibrate_scores(results)
    
    # 4. 过滤层
    hs300_trend = ctx.get_hs300_trend()
    _apply_filters(results, td, hs300_trend)
    
    # 5. 入库
    from p6_dual_track_engine import daily_pipeline_insert
    
    conn = get_connection()
    cur = conn.cursor()
    saved, skipped = 0, 0
    
    for r in results:
        try:
            code = r['ts_code']
            
            # 读取缠论数据
            cur.execute("""
                SELECT buy_sell_point, zoushi_type, beichi_type, structure_score,
                       autumn_tiger, tiger_confidence
                FROM chanlun_structure
                WHERE ts_code=%s AND trade_date=%s
                ORDER BY trade_date DESC LIMIT 1
            """, (code, td))
            cl = cur.fetchone()
            
            bs = cl['buy_sell_point'] if cl and cl.get('buy_sell_point') else 'none'
            zt = cl['zoushi_type'] if cl and cl.get('zoushi_type') else '未知'
            ss = float(cl['structure_score'] or 0) if cl else 0
            autumn = 1 if (cl and cl['autumn_tiger']) else 0
            tiger_conf = float(cl['tiger_confidence'] or 0) if cl else 0
            
            calib = float(r['calibrated_score'] or 0)
            if calib >= 75:
                op_mode = 'attack'
            elif calib >= 60:
                op_mode = 'normal'
            elif calib >= 40:
                op_mode = 'defense'
            else:
                op_mode = 'dormant'
            
            if calib >= 80:
                sig_conf = 'high'
            elif calib >= 60:
                sig_conf = 'medium'
            else:
                sig_conf = 'low'
            
            track_label = '动量' if r['track'] == 'momentum' else '回归'
            reason_parts = [f"{ctx.season}+{ctx.regime}", f"{track_label}轨道"]
            if bs and bs != 'none':
                reason_parts.append(f"{bs}确认")
            if zt and zt not in ('unknown', '未知'):
                reason_parts.append(zt)
            if ss >= 80:
                reason_parts.append('结构强势')
            elif ss >= 60:
                reason_parts.append('结构稳定')
            if autumn:
                reason_parts.append('秋老虎')
            reason = '+'.join(reason_parts)
            
            det = r.get('details', {}) or {}
            p_score = float(det.get('penalty_score', 0) or 0)
            p_reason = det.get('penalty_reason', '')
            
            stf = r.get('stf', {}) or {}
            
            cur.execute("""
                INSERT INTO strategy_signal 
                    (ts_code, trade_date, track, composite_score, calibrated_score,
                     scoring_strategy, direction, operation_mode, buy_sell_point,
                     reason_chain, signal_confidence, autumn_tiger, tiger_confidence,
                     hengjiyuan_level, season,
                     penalty_score, penalty_reason,
                     short_term_score, stf_capital, stf_volume, stf_overbought, stf_momentum)
                VALUES (%s, %s, %s, %s, %s, %s, 'dual_track_v1', %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    track=VALUES(track), composite_score=VALUES(composite_score),
                    calibrated_score=VALUES(calibrated_score),
                    scoring_strategy=VALUES(scoring_strategy),
                    operation_mode=VALUES(operation_mode),
                    buy_sell_point=VALUES(buy_sell_point),
                    reason_chain=VALUES(reason_chain),
                    signal_confidence=VALUES(signal_confidence),
                    autumn_tiger=VALUES(autumn_tiger),
                    tiger_confidence=VALUES(tiger_confidence),
                    hengjiyuan_level=VALUES(hengjiyuan_level),
                    season=VALUES(season),
                    penalty_score=VALUES(penalty_score),
                    penalty_reason=VALUES(penalty_reason),
                    short_term_score=VALUES(short_term_score),
                    stf_capital=VALUES(stf_capital), stf_volume=VALUES(stf_volume),
                    stf_overbought=VALUES(stf_overbought), stf_momentum=VALUES(stf_momentum)
            """, (code, td, r['track'],
                  r['score'], r['calibrated_score'],
                  'momentum' if r['track'] == 'momentum' else 'reversion',
                  op_mode, bs, reason, sig_conf,
                  autumn, tiger_conf,
                  ctx.raw.get('hengjiyuan_level', 'weak_heng'),
                  ctx.season,
                  p_score, p_reason,
                  stf.get('short_term_score', 50), stf.get('capital_inertia', 50),
                  stf.get('volume_health', 50), stf.get('overbought_safety', 50),
                  stf.get('short_momentum', 50)))
            saved += 1
        except Exception as e:
            skipped += 1
            if skipped <= 3:
                print(f"    ⚠️ 跳过 {r['ts_code']}: {e}")
    
    try:
        conn.commit()
    except:
        pass
    try:
        cur.close()
    except:
        pass
    try:
        conn.close()
    except:
        pass
    
    return saved, skipped


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else '2024-09-02'
    end = sys.argv[2] if len(sys.argv) > 2 else '2026-07-20'
    
    print(f"🚀 V13.3e 历史评分回填")
    print(f"   范围: {start} ~ {end}")
    
    # 获取所有交易日
    print("获取交易日列表...")
    days = get_trading_days(start, end)
    print(f"   {len(days)}个交易日")
    
    # 获取监控池
    print("获取监控池...")
    codes = get_watch_pool()
    print(f"   {len(codes)}只股票")
    
    # 初始化季节引擎（只一次）
    print("初始化季节引擎...")
    engine = SeasonEngine(use_market_breadth=False)
    
    t0 = time.time()
    total_saved = 0
    total_skipped = 0
    
    for idx, td in enumerate(days):
        day_start = time.time()
        saved, skipped = backfill_single_date(td, codes, engine)
        total_saved += saved
        total_skipped += skipped
        elapsed = time.time() - day_start
        
        if (idx + 1) % 10 == 0:
            pct = (idx + 1) / len(days) * 100
            total_elapsed = time.time() - t0
            eta = (total_elapsed / (idx + 1)) * (len(days) - idx - 1)
            print(f"  📅 {td} ({idx+1}/{len(days)} {pct:.0f}%) "
                  f"保存{saved}/跳过{skipped} | "
                  f"已用{total_elapsed:.0f}s 预估剩余{eta:.0f}s")
    
    total_elapsed = time.time() - t0
    print(f"\n✅ V13.3e 历史回填完成")
    print(f"   总天数: {len(days)}天")
    print(f"   总保存: {total_saved}条")
    print(f"   总跳过: {total_skipped}条")
    print(f"   耗时: {total_elapsed:.0f}s ({total_elapsed/60:.1f}分)")


if __name__ == '__main__':
    main()
