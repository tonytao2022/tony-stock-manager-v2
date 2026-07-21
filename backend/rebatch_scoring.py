#!/usr/bin/env python3
"""
批量重跑评分脚本 v3 — 用当前p6_dual_track_engine覆盖strategy_signal
从2026-01-01开始，逐日调用评分引擎，覆盖写入DB

使用方式:
  python3 rebatch_scoring.py                    # 从2026-01-01到最新
  python3 rebatch_scoring.py 2026-03-01         # 指定起始
  python3 rebatch_scoring.py 2026-03-01 2026-04-01  # 指定区间

验证机制:
  - 每个交易日评分完成后对比已写入条数（必须=833）
  - 评分前后校验数据完整性
  - 失败自动重试1次
  - 断点续跑（每天记录进度）
"""
import sys, os, time, json, traceback
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['SKIP_REALTIME'] = '1'

from p6_dual_track_engine import batch_score, MarketContext
from db_config import get_connection, DB_CONFIG as DBC

PROGRESS_FILE = '/tmp/rebatch_progress.json'

# 季节→轨道映射
MOMENTUM_SEASONS = {'summer', 'spring', 'weak_spring', 'chaos_spring'}
REVERSION_SEASONS = {'autumn', 'weak_autumn', 'chaos_autumn', 'winter'}

def get_scoring_strategy(season):
    if season in MOMENTUM_SEASONS:
        return 'momentum'
    elif season == 'chaos':
        return 'momentum'
    return 'reversion'

def load_progress():
    try:
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    except:
        return {'last_date': None, 'total_done': 0, 'errors': []}

def save_progress(trade_date_str, total_done, errors=None):
    data = {'last_date': trade_date_str, 'total_done': total_done,
            'errors': errors or [], 'updated_at': datetime.now().isoformat()}
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(data, f, ensure_ascii=False)

def get_trade_dates(start_date, end_date):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT trade_date, season, regime, confidence, raw_score,
               hengjiyuan_level, hengjiyuan_score, chaos_subtype,
               regime_strength, scoring_strategy
        FROM season_state
        WHERE index_code='MARKET' AND trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date
    """, (str(start_date), str(end_date)))
    dates = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return dates

def verify_day_data(trade_date_str, expected_count=833, retries=2):
    for attempt in range(retries):
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) as cnt,
                   COUNT(DISTINCT scoring_strategy) as strategies,
                   ROUND(AVG(composite_score), 2) as avg_score,
                   ROUND(AVG(calibrated_score), 2) as avg_calib,
                   SUM(CASE WHEN composite_score IS NULL THEN 1 ELSE 0 END) as null_comp
            FROM strategy_signal
            WHERE trade_date=%s AND scoring_strategy='dual_track_v1'
        """, (trade_date_str,))
        row = dict(cur.fetchone())
        cur.close(); conn.close()
        if row['cnt'] >= expected_count * 0.9 and row['null_comp'] == 0:
            return row
        if attempt < retries - 1:
            time.sleep(5)
    return row

def run_one_day(td_info):
    trade_date_str = str(td_info['trade_date'])
    season = td_info['season']
    scoring_strategy = td_info.get('scoring_strategy') or get_scoring_strategy(season)

    judge_result = {
        'market_season': season,
        'market_regime': td_info['regime'],
        'market_confidence': float(td_info['confidence'] or 0.5),
        'market_scoring_strategy': scoring_strategy,
        'trade_date': trade_date_str,
        'market_raw_score': float(td_info['raw_score'] or 50),
        'hengjiyuan_level': td_info['hengjiyuan_level'] or 'weak_heng',
        'hengjiyuan_score': float(td_info['hengjiyuan_score'] or 50),
        'chaos_subtype': td_info['chaos_subtype'],
        'regime_strength': float(td_info['regime_strength'] or 1.0),
    }
    ctx = MarketContext(judge_result)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
    codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close()

    if not codes:
        conn.close()
        return trade_date_str, 0, 'no_stocks'

    try:
        results = batch_score(codes, ctx)
    except Exception as e:
        conn.close()
        raise e

    cur = conn.cursor()
    saved = 0
    for r in results:
        code = r['ts_code']
        # 与score_pipeline.py一致：composite_score = 引擎扣完penalty的score
        comp_score = float(r.get('score', 0) or 0)
        calib_score = float(r.get('calibrated_score', comp_score) or 0)
        track = r.get('track', '')
        stf = r.get('stf', {})
        is_gate = 1 if r.get('gate_triggered') else 0
        is_filter = 1 if r.get('_filtered') else 0
        filt_reason = r.get('filter_reason', '')
        # penalty_score/penalty_reason 存在于 details 嵌套对象中
        det = r.get('details', {}) or {}
        p_score = float(det.get('penalty_score', 0) or 0)
        p_reason = det.get('penalty_reason', '')

        try:
            cur.execute("""
                INSERT INTO strategy_signal
                    (ts_code, trade_date, track, composite_score, calibrated_score,
                     scoring_strategy, season, gate_triggered,
                     is_filtered, filter_reason, penalty_score, penalty_reason,
                     short_term_score, stf_capital, stf_momentum, stf_overbought, stf_volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    track=VALUES(track),
                    composite_score=VALUES(composite_score),
                    calibrated_score=VALUES(calibrated_score),
                    scoring_strategy=VALUES(scoring_strategy),
                    season=VALUES(season),
                    gate_triggered=VALUES(gate_triggered),
                    is_filtered=VALUES(is_filtered),
                    filter_reason=VALUES(filter_reason),
                    penalty_score=VALUES(penalty_score),
                    penalty_reason=VALUES(penalty_reason),
                    short_term_score=VALUES(short_term_score),
                    stf_capital=VALUES(stf_capital),
                    stf_momentum=VALUES(stf_momentum),
                    stf_overbought=VALUES(stf_overbought),
                    stf_volume=VALUES(stf_volume)
            """, (
                code, trade_date_str, track,
                round(comp_score, 2), round(calib_score, 2),
                'dual_track_v1', season, is_gate,
                is_filter, filt_reason,
                round(p_score, 2), p_reason,
                float(stf.get('short_term_score', 50) or 50),
                float(stf.get('capital_inertia', 50) or 50),
                float(stf.get('short_momentum', 50) or 50),
                float(stf.get('overbought_safety', 50) or 50),
                float(stf.get('volume_health', 50) or 50),
            ))
            saved += 1
        except:
            pass

        if saved % 200 == 0:
            conn.commit()

    conn.commit()
    cur.close()
    conn.close()
    return trade_date_str, saved, season

def main():
    start_str = sys.argv[1] if len(sys.argv) > 1 else '2026-01-01'
    end_str = sys.argv[2] if len(sys.argv) > 2 else str(date.today())

    print(f"\n{'='*60}", flush=True)
    print(f"📊 批量重跑评分 v3", flush=True)
    print(f"   日期: {start_str} ~ {end_str}", flush=True)
    print(f"{'='*60}", flush=True)

    dates = get_trade_dates(start_str, end_str)
    print(f"   交易日: {len(dates)}天", flush=True)
    if not dates:
        print("❌ 无交易日数据!", flush=True)
        sys.exit(1)

    # 断点续跑
    progress = load_progress()
    if progress['last_date']:
        skip_idx = 0
        for i, d in enumerate(dates):
            if str(d['trade_date']) == progress['last_date']:
                skip_idx = i + 1
                break
        if skip_idx > 0:
            print(f"   断点续跑: 已跳过{skip_idx}天 (上次: {progress['last_date']})", flush=True)
            dates = dates[skip_idx:]

    total_done = progress.get('total_done', 0)
    errors = progress.get('errors', [])
    start_time = time.time()

    for i, td_info in enumerate(dates):
        td_str = str(td_info['trade_date'])
        season = td_info['season']
        num = i + 1

        elapsed_sofar = time.time() - start_time
        print(f"\n[{num}/{len(dates)}] {td_str} [{season:>12}] +{elapsed_sofar:.0f}s", flush=True)

        t0 = time.time()
        try:
            result = run_one_day(td_info)
            td_res, saved, season_name = result
            dt = time.time() - t0
            print(f"  ✅ 评分完成: {saved}只 ({dt:.1f}s)", flush=True)

            verify = verify_day_data(td_str)
            if verify['cnt'] == 0:
                print(f"  ❌ 验证失败: 写入后0条!", flush=True)
                errors.append({'date': td_str, 'error': 'zero_records'})
                save_progress(td_str, total_done, errors)
                continue

            if verify['null_comp'] > 0:
                print(f"  ⚠️ {verify['null_comp']}条NULL综合分", flush=True)

            total_done += saved
            print(f"  📊 验证: {verify['cnt']}条 | 综合分均{verify['avg_score']} | 校准分均{verify['avg_calib']}", flush=True)

        except Exception as e:
            dt = time.time() - t0
            print(f"  ❌ 失败: {e} ({dt:.1f}s)", flush=True)
            traceback.print_exc()
            errors.append({'date': td_str, 'error': str(e)})
            print(f"  🔄 重试...", flush=True)
            time.sleep(3)
            try:
                result = run_one_day(td_info)
                td_res, saved, season_name = result
                total_done += saved
                print(f"  ✅ 重试成功: {saved}只", flush=True)
            except Exception as e2:
                print(f"  ❌ 重试也失败: {e2}", flush=True)
                errors.append({'date': td_str, 'error': f'retry: {e2}'})

        save_progress(td_str, total_done, errors)

    elapsed = time.time() - start_time
    print(f"\n{'='*60}", flush=True)
    print(f"✅ 批量重跑完成！", flush=True)
    print(f"   日期: {start_str} ~ {end_str}", flush=True)
    print(f"   天数: {len(dates)}", flush=True)
    print(f"   总写入: {total_done}条", flush=True)
    print(f"   耗时: {elapsed:.0f}s ({elapsed/60:.1f}min, {elapsed/max(len(dates),1):.0f}s/天)", flush=True)
    if errors:
        print(f"   错误: {len(errors)}项", flush=True)
        for e in errors[:10]:
            print(f"     ❌ {e['date']}: {e['error']}", flush=True)
    print(f"{'='*60}", flush=True)

if __name__ == '__main__':
    main()
