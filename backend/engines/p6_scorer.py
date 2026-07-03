#!/usr/bin/env python3
"""
p6_scorer.py - 评分管道入口（被 daily_orch.py _step_score 调用）

包装 p6_dual_track_engine.daily_pipeline()
提供简单的 run_scoring(trade_date) 接口
"""
import os, sys, logging
from datetime import date, datetime

logger = logging.getLogger('p6_scorer')


def run_scoring(trade_date=None):
    """
    执行评分管道（OOM-safe，分批评分）

    Args:
        trade_date: date对象或None（自动取最新交易日）
    """
    # 确保能找到backend模块
    backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)

    if trade_date is None:
        from datetime import date as _dt
        trade_date = _dt.today()

    from db_config import get_connection

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
    all_stocks = [r['ts_code'] for r in cur.fetchall()]
    cur.close()
    conn.close()

    total = len(all_stocks)
    logger.info(f'[P6Scorer] 开始评分: {total}只股票, 分批每批100只')

    batch_size = 100
    all_results = []

    for i in range(0, total, batch_size):
        batch = all_stocks[i:i + batch_size]
        logger.info(f'[P6Scorer] 批次 {i//batch_size + 1}/{(total-1)//batch_size + 1}: {batch[0]}...{batch[-1]} ({len(batch)}只)')

        # 每批写入临时表，防OOM
        _write_batch(batch, trade_date)

        # 每批后释放内存
        import gc
        gc.collect()

    logger.info(f'[P6Scorer] 评分完成: {total}只')

    return {'total': total, 'trade_date': str(trade_date)}


def _write_batch(ts_codes, trade_date):
    """
    写入一批股票的评分数据到strategy_signal
    复用p6_dual_track的评分逻辑，但逐只评分+逐条写入
    """
    backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)

    from p6_dual_track_engine import MarketContext, score_stock, calibrate_scores
    from season_engine import SeasonEngine
    from db_config import get_connection

    # 1. 季节判定（只一次）
    engine = SeasonEngine()
    judge_result = engine.judge_market_season()
    ctx = MarketContext(judge_result)

    # 2. 逐只评分
    results = []
    for code in ts_codes:
        try:
            r = score_stock(code, ctx)
            results.append(r)
        except Exception as e:
            logger.warning(f'[P6Scorer] {code} 评分失败: {e}')
            continue

    # 3. 校准
    calibrate_scores(results)

    # 4. 入库
    conn = get_connection()
    cur = conn.cursor()

    for r in results:
        code = r['ts_code']
        track = r.get('track', 'momentum')
        score = r.get('score', 50)
        cal = r.get('calibrated_score', 50)
        strategy = ctx.scoring_strategy
        sig_conf = r.get('signal_confidence', 0.5)
        stf_tier = r.get('stf_tier', 'A')
        stf_data = r.get('stf', {})

        cur.execute("""
            INSERT INTO strategy_signal
                (ts_code, trade_date, track, composite_score, calibrated_score,
                 scoring_strategy, direction, operation_mode,
                 signal_confidence, hengjiyuan_level,
                 short_term_score, stf_capital, stf_volume, stf_overbought, stf_momentum)
            VALUES (%s, %s, %s, %s, %s, %s, 'p6_v4', %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                track=VALUES(track),
                composite_score=VALUES(composite_score),
                calibrated_score=VALUES(calibrated_score),
                scoring_strategy=VALUES(scoring_strategy),
                operation_mode=VALUES(operation_mode),
                signal_confidence=VALUES(signal_confidence),
                hengjiyuan_level=VALUES(hengjiyuan_level),
                short_term_score=VALUES(short_term_score),
                stf_capital=VALUES(stf_capital), stf_volume=VALUES(stf_volume),
                stf_overbought=VALUES(stf_overbought), stf_momentum=VALUES(stf_momentum)
        """, (
            code, str(trade_date),
            track, round(float(score), 2), round(float(cal), 2),
            strategy,
            r.get('_stf_tier_label', ''),
            round(float(sig_conf), 2),
            ctx.raw.get('hengjiyuan_level', '混沌纪元'),
            round(float(stf_data.get('short_term_score', 50)), 2),
            round(float(stf_data.get('capital_inertia', 50)), 2),
            round(float(stf_data.get('volume_health', 50)), 2),
            round(float(stf_data.get('overbought_safety', 50)), 2),
            round(float(stf_data.get('short_momentum', 50)), 2),
        ))

    conn.commit()
    cur.close()
    conn.close()

    logger.info(f'[P6Scorer] 批次入库: {len(results)}只')

    return results


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(name)s %(message)s')
    run_scoring()
