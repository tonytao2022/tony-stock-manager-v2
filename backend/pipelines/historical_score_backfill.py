#!/usr/bin/env python3
"""
历史评分回填脚本 v3 — 用P6双轨引擎对历史交易日逐日评分+百分位校准
写入 backtest_score_daily 表，与 score_pipeline.py 逻辑一致

用法: python3 pipelines/historical_score_backfill.py
"""
import sys, os, time, math, pymysql, traceback
from datetime import date, timedelta, datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from p6_dual_track_engine import score_stock, MarketContext

DB_CFG = {
    'host': '127.0.0.1', 'port': 3306, 'user': 'debian-sys-maint',
    'password': 'iXve1rVBXfdA4tL9', 'database': 'stock_db_v2',
    'charset': 'utf8mb4', 'connect_timeout': 10,
    'cursorclass': pymysql.cursors.DictCursor,
}

def q(sql, params=None):
    """快速查询，返回结果列表"""
    conn = pymysql.connect(**DB_CFG)
    cur = conn.cursor()
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows

def q_one(sql, params=None):
    rows = q(sql, params)
    return rows[0] if rows else None

def e(sql, params=None):
    """执行SQL（写操作）"""
    conn = pymysql.connect(**DB_CFG)
    cur = conn.cursor()
    cur.execute(sql, params or ())
    conn.commit()
    cur.close(); conn.close()

def build_calib_map(scores):
    """百分位校准映射 — 与score_pipeline.py完全一致"""
    n = len(scores)
    if n == 0:
        return {}
    sorted_scores = sorted(scores)
    targets = {
        5: 10, 10: 15, 15: 18, 20: 20, 25: 22, 30: 24,
        35: 26, 40: 28, 45: 29, 50: 30, 55: 32,
        60: 34, 65: 36, 70: 38, 75: 40, 80: 44,
        85: 48, 90: 50, 93: 55, 95: 60, 97: 68, 99: 75, 100: 80,
    }
    mapping = {}
    for pct, target in targets.items():
        idx = min(n - 1, int(math.ceil(n * pct / 100)) - 1)
        mapping[sorted_scores[idx]] = target
    return mapping

def calib_score(raw_score, calib_map):
    calibrated = raw_score
    for threshold in sorted(calib_map.keys(), reverse=True):
        if raw_score >= threshold:
            calibrated = calib_map[threshold]
            break
    return calibrated

def run():
    start_date = '2024-09-01'
    end_date = '2026-06-18'

    print(f"📅 回填历史评分: {start_date} ~ {end_date}", flush=True)

    # 1. 交易日
    rows = q("SELECT DISTINCT trade_date FROM daily_kline_qfq WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date",
             (start_date, end_date))
    trade_days = [str(r['trade_date']) for r in rows]
    print(f"📆 共 {len(trade_days)} 个交易日", flush=True)

    # 2. 股票池
    rows = q("SELECT ts_code FROM watch_pool WHERE is_active=1")
    pool = [r['ts_code'] for r in rows]
    print(f"📈 监控池: {len(pool)} 只", flush=True)

    total_saved = 0
    total_skipped = 0
    t_start = time.time()

    for day_idx, td_str in enumerate(trade_days):
        day_t0 = time.time()
        show = ((day_idx + 1) % 10 == 0) or (day_idx == 0) or (day_idx == len(trade_days) - 1)

        # 当日季节
        r = q_one("SELECT season FROM season_state WHERE index_code='MARKET' AND trade_date=%s", (td_str,))
        mkt_season = r['season'] if r else 'chaos'
        scoring_strategy = 'momentum' if mkt_season in ('summer', 'spring', 'chaos_spring') else 'reversion'

        # MarketContext
        market_info = MarketContext({'season': mkt_season, 'regime': 'range',
                                     'trade_date': td_str, 'scoring_strategy': scoring_strategy})

        # 逐个评分
        day_results = []  # [{ts_code, raw_score, track, details, close_price, season}, ...]
        day_skipped = 0

        for code in pool:
            try:
                # 收盘价
                r_cp = q_one("SELECT `close` FROM daily_kline_qfq WHERE ts_code=%s AND trade_date=%s",
                            (code, td_str))
                if not r_cp or not r_cp.get('close'):
                    day_skipped += 1
                    continue
                cp = float(r_cp['close'])

                # P6评分
                result = score_stock(code, market_info)
                raw_score = float(result.get('score', 0))
                if raw_score <= 0:
                    day_skipped += 1
                    continue

                day_results.append({
                    'ts_code': code,
                    'raw_score': raw_score,
                    'track': result.get('track', ''),
                    'details': result.get('details', {}) or {},
                    'close_price': cp,
                })
            except Exception:
                day_skipped += 1

        if not day_results:
            if show:
                print(f"[{day_idx+1}/{len(trade_days)}] {td_str} ⚠️ 全部跳过({day_skipped})", flush=True)
            total_skipped += day_skipped
            continue

        # 百分位校准
        raw_scores = [r2['raw_score'] for r2 in day_results]
        calib_map = build_calib_map(raw_scores)
        calibs = [calib_score(r2['raw_score'], calib_map) for r2 in day_results]
        high_count = len([c for c in calibs if c >= 68])

        # 批量入库
        insert_sql = """INSERT INTO backtest_score_daily
            (ts_code, trade_date, track, composite_score, calibrated_score,
             chanlun_trend, structure_score, momentum_score, pos_score,
             mf_score, margin_score, season, close_price)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            composite_score=VALUES(composite_score),
            calibrated_score=VALUES(calibrated_score),
            track=VALUES(track),
            season=VALUES(season),
            close_price=VALUES(close_price)"""

        batch = []
        for idx, item in enumerate(day_results):
            d = item['details']
            batch.append((
                item['ts_code'], td_str, item['track'],
                round(item['raw_score'], 1), round(calibs[idx], 1),
                round(float(d.get('chanlun_trend', 0) or 0), 1),
                round(float(d.get('structure_score', 0) or 0), 1),
                round(float(d.get('momentum_raw', 0) or 0), 1),
                round(float(d.get('pos_score', 0) or 0), 1),
                round(float(d.get('mf_score', 0) or 0), 1),
                round(float(d.get('margin_score', 0) or 0), 1),
                mkt_season, item['close_price'],
            ))

        conn = pymysql.connect(**DB_CFG)
        cur = conn.cursor()
        try:
            cur.executemany(insert_sql, batch)
            conn.commit()
        finally:
            cur.close(); conn.close()

        total_saved += len(batch)
        total_skipped += day_skipped

        if show:
            elapsed = time.time() - t_start
            pct = (day_idx + 1) / len(trade_days) * 100
            eta = elapsed / (day_idx + 1) * (len(trade_days) - day_idx - 1)
            eta_str = f"{eta/3600:.1f}h" if eta > 3600 else f"{eta/60:.0f}min"
            print(
                f"[{day_idx+1}/{len(trade_days)}] {pct:.0f}% | {td_str} | "
                f"入{len(batch)}条 | Σ{total_saved}条 | "
                f"校准≥68:{high_count}只 | "
                f"ETA~{eta_str}", flush=True)

    elapsed = time.time() - t_start
    print(f"\n✅ 完成！总写入: {total_saved}条 | 跳过: {total_skipped}条 | 耗时: {elapsed/60:.0f}min", flush=True)

if __name__ == '__main__':
    t0 = time.time()
    run()
    print(f"⏱️ 总耗时: {(time.time()-t0)/60:.1f}min", flush=True)
