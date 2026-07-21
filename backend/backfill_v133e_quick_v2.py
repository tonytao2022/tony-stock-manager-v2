#!/usr/bin/env python3
"""
V13.3e 历史评分快速重填 v2
—— 使用 executemany 批量写入，每次100条commit一次
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, '/opt/stock-analyzer')
from datetime import date, timedelta
from typing import List, Dict
import pymysql

from p6_dual_track_engine import (
    MarketContext, score_stock, calibrate_scores, _apply_filters
)
from db_config import get_connection, DB_CONFIG


def get_historical_seasons(start: str, end: str):
    """从 season_state 获取每日季节"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT trade_date FROM season_state
        WHERE index_code = 'MARKET'
          AND trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date
    """, (start, end))
    dates_list = [str(r['trade_date']) for r in cur.fetchall()]

    season_map = {}
    for td in dates_list:
        cur.execute("""
            SELECT season, regime, scoring_strategy, hengjiyuan_level, hengjiyuan_score
            FROM season_state WHERE index_code = 'MARKET' AND trade_date = %s
            LIMIT 1
        """, (td,))
        row = cur.fetchone()
        if row:
            season_map[td] = {
                'market_season': row['season'],
                'market_regime': row['regime'],
                'market_scoring_strategy': row['scoring_strategy'],
                'hengjiyuan_level': row['hengjiyuan_level'],
                'hengjiyuan_score': float(row['hengjiyuan_score'] or 0),
                'market_confidence': 0.7,
                'index_details': {}
            }
    cur.close(); conn.close()
    return season_map, dates_list


def get_watch_pool():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
    codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close(); conn.close()
    return codes


def get_chanlun_batch(td: str, codes: List[str]):
    """批量读取缠论数据"""
    conn = get_connection()
    cur = conn.cursor()
    cl_map = {}
    for code in codes:
        cur.execute("""
            SELECT ts_code, buy_sell_point, zoushi_type, structure_score,
                   autumn_tiger, tiger_confidence
            FROM chanlun_structure
            WHERE ts_code=%s AND trade_date=%s
            ORDER BY trade_date DESC LIMIT 1
        """, (code, td))
        c = cur.fetchone()
        cl_map[code] = c or {}
    cur.close(); conn.close()
    return cl_map


def build_rows(results, td, ctx, cl_map):
    """构建批量INSERT行"""
    rows = []
    for r in results:
        code = r['ts_code']
        cl = cl_map.get(code, {})
        
        bs = cl.get('buy_sell_point') or 'none'
        ss = float(cl.get('structure_score', 0) or 0)
        autumn = 1 if cl.get('autumn_tiger') else 0
        tiger_conf = float(cl.get('tiger_confidence', 0) or 0)
        
        calib = float(r['calibrated_score'] or 0)
        op_mode = 'attack' if calib >= 75 else ('normal' if calib >= 60 else ('defense' if calib >= 40 else 'dormant'))
        sig_conf = 'high' if calib >= 80 else ('medium' if calib >= 60 else 'low')
        
        track_label = '动量' if r['track'] == 'momentum' else '回归'
        reason_parts = [f"{ctx.season}+{ctx.regime}", f"{track_label}轨道"]
        if bs and bs != 'none': reason_parts.append(f"{bs}确认")
        if ss >= 80: reason_parts.append('结构强势')
        elif ss >= 60: reason_parts.append('结构稳定')
        if autumn: reason_parts.append('秋老虎')
        reason = '+'.join(reason_parts)
        
        det = r.get('details', {}) or {}
        p_score = float(det.get('penalty_score', 0) or 0)
        p_reason = det.get('penalty_reason', '')
        
        stf = r.get('stf', {}) or {}
        
        rows.append((
            code, td, r['track'],
            r['score'], r['calibrated_score'],
            'momentum' if r['track'] == 'momentum' else 'reversion',
            op_mode, bs, reason, sig_conf,
            autumn, tiger_conf,
            ctx.raw.get('hengjiyuan_level', 'weak_heng'),
            ctx.season,
            p_score, p_reason,
            stf.get('short_term_score', 50), stf.get('capital_inertia', 50),
            stf.get('volume_health', 50), stf.get('overbought_safety', 50),
            stf.get('short_momentum', 50)
        ))
    return rows


def process_and_insert(td, codes, ctx):
    """评分 + 写入"""
    # 评分
    results = []
    for code in codes:
        r = score_stock(code, ctx)
        results.append(r)
    calibrate_scores(results)
    
    hs300_trend = ctx.get_hs300_trend()
    _apply_filters(results, td, hs300_trend)
    
    # 获取缠论
    cl_map = get_chanlun_batch(td, codes)
    
    # 构建行
    rows = build_rows(results, td, ctx, cl_map)
    if not rows:
        return 0, 0, results
    
    # 批量写入
    sql = """INSERT INTO strategy_signal 
        (ts_code, trade_date, track, composite_score, calibrated_score,
         scoring_strategy, direction, operation_mode, buy_sell_point,
         reason_chain, signal_confidence, autumn_tiger, tiger_confidence,
         hengjiyuan_level, season,
         penalty_score, penalty_reason,
         short_term_score, stf_capital, stf_volume, stf_overbought, stf_momentum)
    VALUES (%s, %s, %s, %s, %s, %s, 'dual_track_v1', %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
        composite_score=VALUES(composite_score), calibrated_score=VALUES(calibrated_score),
        track=VALUES(track), scoring_strategy=VALUES(scoring_strategy),
        operation_mode=VALUES(operation_mode), buy_sell_point=VALUES(buy_sell_point),
        reason_chain=VALUES(reason_chain), signal_confidence=VALUES(signal_confidence),
        autumn_tiger=VALUES(autumn_tiger), tiger_confidence=VALUES(tiger_confidence),
        hengjiyuan_level=VALUES(hengjiyuan_level), season=VALUES(season),
        penalty_score=VALUES(penalty_score), penalty_reason=VALUES(penalty_reason),
        short_term_score=VALUES(short_term_score), stf_capital=VALUES(stf_capital),
        stf_volume=VALUES(stf_volume), stf_overbought=VALUES(stf_overbought),
        stf_momentum=VALUES(stf_momentum)"""
    
    conn = get_connection()
    cur = conn.cursor()
    saved = 0
    try:
        for i in range(0, len(rows), 200):
            batch = rows[i:i+200]
            cur.executemany(sql, batch)
            conn.commit()
            saved += len(batch)
        cur.close()
        conn.close()
    except Exception as e:
        try: cur.close()
        except: pass
        try: conn.close()
        except: pass
        print(f"    ⚠️ 写入失败: {e}")
        return 0, len(rows), results
    
    return saved, len(rows) - saved, results


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else '2024-09-02'
    end = sys.argv[2] if len(sys.argv) > 2 else '2026-07-17'
    
    print(f"🚀 V13.3e 历史评分快速重填 v2")
    print(f"   范围: {start} ~ {end}")
    print(f"   开始: {time.strftime('%H:%M:%S')}")
    
    print("获取季节...")
    season_map, days_list = get_historical_seasons(start, end)
    print(f"   {len(days_list)}个交易日")
    
    print("获取监控池...")
    codes = get_watch_pool()
    print(f"   {len(codes)}只")
    
    t0 = time.time()
    total_saved, total_skipped = 0, 0
    
    for idx, td in enumerate(days_list):
        t1 = time.time()
        
        ctx_raw = season_map[td]
        ctx_raw['trade_date'] = td
        ctx = MarketContext(ctx_raw)
        ctx.trade_date = td
        
        saved, skipped, results = process_and_insert(td, codes, ctx)
        total_saved += saved
        total_skipped += skipped
        elapsed = time.time() - t1
        
        if (idx+1) % 10 == 0 or idx == 0:
            scores = [r['score'] for r in results]
            avg_s = sum(scores)/len(scores) if scores else 0
            ge60 = sum(1 for s in scores if s >= 60)
            ge55 = sum(1 for s in scores if s >= 55)
            ge50 = sum(1 for s in scores if s >= 50)
            pct = (idx+1)/len(days_list)*100
            total_elapsed = time.time() - t0
            eta = total_elapsed/(idx+1)*(len(days_list)-idx-1)
            print(f"  {td} ({idx+1}/{len(days_list)} {pct:.0f}%) "
                  f"avg{avg_s:.0f} ≥60={ge60} ≥50={ge50} "
                  f"保存{saved} 耗时{elapsed:.0f}s | "
                  f"总{total_elapsed:.0f}s ETA{eta:.0f}s")
    
    total_elapsed = time.time() - t0
    print(f"\n✅ 完成! {total_saved}条 保存 | {total_skipped}条 跳过")
    print(f"   耗时: {total_elapsed:.0f}s ({total_elapsed/60:.1f}分)")
    print(f"   结束: {time.strftime('%H:%M:%S')}")


if __name__ == '__main__':
    main()
