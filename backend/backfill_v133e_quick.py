#!/usr/bin/env python3
"""
V13.3e 历史评分快速重填 —— 直接读取已有trade_dates + season_state
用法: nohup python3 backfill_v133e_quick.py > backfill_133e.log 2>&1 &
"""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, '/opt/stock-analyzer')
from datetime import datetime, date
from typing import List, Dict

from p6_dual_track_engine import (
    MarketContext, score_stock, calibrate_scores, _apply_filters
)
from db_config import get_connection

def get_historical_seasons(start, end):
    """从 season_state 表获取每个交易日的季节判定"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT trade_date FROM season_state
        WHERE index_code = 'MARKET'
          AND trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date
    """, (start, end))
    dates_list = [str(r['trade_date']) for r in cur.fetchall()]
    
    # 对每个交易日，获取综合季节
    season_map = {}
    for td in dates_list:
        # 获取 MARKET 行
        cur.execute("""
            SELECT season, regime, scoring_strategy, hengjiyuan_level, hengjiyuan_score
            FROM season_state
            WHERE index_code = 'MARKET' AND trade_date = %s
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
    return season_map


def get_watch_pool():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT ts_code FROM watch_pool WHERE is_active=1")
    codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close(); conn.close()
    return codes


def build_insert_single(conn, r, td, ctx, cl):
    """单条入库"""
    cur = conn.cursor()
    try:
        code = r['ts_code']
        
        bs = cl['buy_sell_point'] if cl and cl.get('buy_sell_point') else 'none'
        zt = cl['zoushi_type'] if cl and cl.get('zoushi_type') else '未知'
        ss = float(cl['structure_score'] or 0) if cl else 0
        autumn = 1 if (cl and cl['autumn_tiger']) else 0
        tiger_conf = float(cl['tiger_confidence'] or 0) if cl else 0
        
        calib = float(r['calibrated_score'] or 0)
        if calib >= 75: op_mode = 'attack'
        elif calib >= 60: op_mode = 'normal'
        elif calib >= 40: op_mode = 'defense'
        else: op_mode = 'dormant'
        
        if calib >= 80: sig_conf = 'high'
        elif calib >= 60: sig_conf = 'medium'
        else: sig_conf = 'low'
        
        track_label = '动量' if r['track'] == 'momentum' else '回归'
        reason_parts = [f"{ctx.season}+{ctx.regime}", f"{track_label}轨道"]
        if bs and bs != 'none': reason_parts.append(f"{bs}确认")
        if zt and zt not in ('unknown', '未知'): reason_parts.append(zt)
        if ss >= 80: reason_parts.append('结构强势')
        elif ss >= 60: reason_parts.append('结构稳定')
        if autumn: reason_parts.append('秋老虎')
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
        cur.close()
        return True
    except Exception as e:
        try: cur.close()
        except: pass
        return False


def process_day(td: str, codes: List[str], season_info: dict):
    """处理单日"""
    # 构造 market context
    ctx_raw = {
        'trade_date': td,
        'market_season': season_info['market_season'],
        'market_regime': season_info['market_regime'],
        'market_scoring_strategy': season_info['market_scoring_strategy'],
        'market_confidence': season_info['market_confidence'],
        'hengjiyuan_level': season_info['hengjiyuan_level'],
        'hengjiyuan_score': season_info['hengjiyuan_score'],
        'index_details': season_info.get('index_details', {})
    }
    ctx = MarketContext(ctx_raw)
    ctx.trade_date = td
    
    # 批量评分
    results = []
    for code in codes:
        r = score_stock(code, ctx)
        results.append(r)
    
    # 校准
    calibrate_scores(results)
    
    # 过滤
    hs300_trend = ctx.get_hs300_trend()
    _apply_filters(results, td, hs300_trend)
    
    # 批量写入
    conn = get_connection()
    
    # 预取当日缠论数据（批量）
    cur = conn.cursor()
    codes_list = [r['ts_code'] for r in results]
    # 按code逐个查
    cl_map = {}
    for code in codes_list:
        cur.execute("""
            SELECT buy_sell_point, zoushi_type, beichi_type, structure_score,
                   autumn_tiger, tiger_confidence
            FROM chanlun_structure
            WHERE ts_code=%s AND trade_date=%s
            ORDER BY trade_date DESC LIMIT 1
        """, (code, td))
        c = cur.fetchone()
        cl_map[code] = c
    cur.close()
    
    saved, skipped = 0, 0
    to_commit = []
    
    for r in results:
        cl = cl_map.get(r['ts_code'])
        ok = build_insert_single(conn, r, td, ctx, cl)
        if ok: saved += 1
        else: skipped += 1
        to_commit.append(r['ts_code'])
        if len(to_commit) % 100 == 0:
            try: conn.commit()
            except: pass
    
    try: conn.commit()
    except: pass
    try: conn.close()
    except: pass
    
    return saved, skipped, results


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else '2024-09-02'
    end = sys.argv[2] if len(sys.argv) > 2 else '2026-07-17'
    
    print(f"🚀 V13.3e 历史评分快速重填")
    print(f"   范围: {start} ~ {end}")
    print(f"   开始时间: {datetime.now().strftime('%H:%M:%S')}")
    
    # 获取历史季节
    print("获取历史季节数据...")
    season_map = get_historical_seasons(start, end)
    days_list = sorted(season_map.keys())
    print(f"   {len(days_list)}个交易日")
    
    # 获取监控池
    print("获取监控池...")
    codes = get_watch_pool()
    print(f"   {len(codes)}只股票")
    
    t0 = time.time()
    total_saved = 0
    total_skipped = 0
    
    for idx, td in enumerate(days_list):
        day_start = time.time()
        saved, skipped, results = process_day(td, codes, season_map[td])
        total_saved += saved
        total_skipped += skipped
        elapsed = time.time() - day_start
        
        # 每10天打一次进度
        if (idx + 1) % 10 == 0:
            pct = (idx + 1) / len(days_list) * 100
            total_elapsed = time.time() - t0
            eta = (total_elapsed / (idx + 1)) * (len(days_list) - idx - 1)
            # 显示当日评分分布概要
            scores = [r['score'] for r in results]
            avg_s = sum(scores)/len(scores) if scores else 0
            ge60 = sum(1 for s in scores if s >= 60)
            ge55 = sum(1 for s in scores if s >= 55)
            ge50 = sum(1 for s in scores if s >= 50)
            print(f"  📅 {td} ({idx+1}/{len(days_list)} {pct:.0f}%) "
                  f"avg={avg_s:.0f} ≥60={ge60} ≥55={ge55} ≥50={ge50} | "
                  f"{elapsed:.1f}s | "
                  f"⏱{total_elapsed:.0f}s ETA{eta:.0f}s")
        
        # 每50天确认一次写入成功
        if (idx + 1) % 50 == 0:
            try:
                conn = get_connection()
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) AS c FROM strategy_signal WHERE trade_date=%s", (td,))
                cnt = cur.fetchone()['c']
                cur.close(); conn.close()
                print(f"     ✅ 确认: {td} 写入 {cnt}条")
            except Exception as e:
                print(f"     ⚠️ 确认失败: {e}")
    
    total_elapsed = time.time() - t0
    print(f"\n✅ V13.3e 历史回填完成")
    print(f"   日期: {len(days_list)}天 ({start} ~ {end})")
    print(f"   总计: {total_saved}条 保存 | {total_skipped}条 跳过")
    print(f"   耗时: {total_elapsed:.0f}s ({total_elapsed/60:.1f}分)")
    print(f"   完成时间: {datetime.now().strftime('%H:%M:%S')}")


if __name__ == '__main__':
    main()
