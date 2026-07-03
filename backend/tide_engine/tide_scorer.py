#!/usr/bin/env python3
"""
tide_scorer.py - Tide评分聚合引擎（核心入口）

流程:
  1. 读watch_pool股票列表
  2. 运行7因子 → tide_factor_value
  3. 运行缠论层 → tide_chanlun_signal
  4. 聚合L3 + 缠论调整 → tide_score_signal
"""
import os, sys, logging
from datetime import date, datetime
from typing import Dict, List, Optional

_backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
if _backend_dir not in sys.path: sys.path.insert(0, _backend_dir)
from db_config import get_connection

logger = logging.getLogger('tide_scorer')


def _get_watch_pool() -> List[str]:
    """获取活跃监控池股票列表"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
    codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close(); conn.close()
    return codes


def _compute_factors(ts_code: str, trade_date: str) -> Dict:
    """计算7个因子"""
    from tide_engine.tide_factor_f1 import compute as f1
    from tide_engine.tide_factor_f2 import compute as f2
    from tide_engine.tide_factor_f3 import compute as f3
    from tide_engine.tide_factor_f4 import compute as f4
    from tide_engine.tide_factor_f5 import compute as f5
    from tide_engine.tide_factor_f6 import compute as f6
    from tide_engine.tide_factor_f7 import compute as f7
    return {
        'f1': f1(ts_code, trade_date),
        'f2': f2(ts_code, trade_date),
        'f3': f3(ts_code, trade_date),
        'f4': f4(ts_code, trade_date),
        'f5': f5(ts_code, trade_date),
        'f6': f6(ts_code, trade_date),
        'f7': f7(ts_code, trade_date),
    }


def _get_base_weights() -> Dict[str, float]:
    """从配置读取权重"""
    from tide_engine.tide_config import get_factor_weights
    return get_factor_weights()


def _get_season(trade_date: str) -> str:
    """获取季节"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT market_season FROM season_state 
            WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 1
        """, (trade_date,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row: return row['market_season']
    except:
        pass
    return 'summer'


def _l3_score(factors: Dict, weights: Dict) -> float:
    """计算L3加权分"""
    score = sum(factors.get(f'f{i}', 0) * weights.get(f'f{i}', 0) for i in range(1, 8))
    # 映射到[0, 100]
    mapped = (score + 5) / 10 * 100
    mapped = max(0, min(100, mapped))
    return round(mapped, 2)


def _save_factor_value(trade_date: str, ts_code: str, factors: Dict, l3: float):
    """写入因子明细表"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tide_factor_value 
            (trade_date, ts_code, f1_score, f2_score, f3_score, f4_score, 
             f5_score, f6_score, f7_score, l3_score)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            f1_score=VALUES(f1_score), f2_score=VALUES(f2_score),
            f3_score=VALUES(f3_score), f4_score=VALUES(f4_score),
            f5_score=VALUES(f5_score), f6_score=VALUES(f6_score),
            f7_score=VALUES(f7_score), l3_score=VALUES(l3_score)
    """, (trade_date, ts_code, factors['f1'], factors['f2'], factors['f3'],
          factors['f4'], factors['f5'], factors['f6'], factors['f7'], l3))
    conn.commit()
    cur.close(); conn.close()


def _save_chanlun_signal(trade_date: str, ts_code: str, signals: Dict):
    """写入缠论信号"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tide_chanlun_signal 
            (trade_date, ts_code, central_breakthrough, divergence, third_buy)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            central_breakthrough=VALUES(central_breakthrough),
            divergence=VALUES(divergence),
            third_buy=VALUES(third_buy)
    """, (trade_date, ts_code,
          int(signals['central']), int(signals['divergence']), int(signals['third_buy'])))
    conn.commit()
    cur.close(); conn.close()


def _save_score_signal(trade_date: str, ts_code: str, l3: float, 
                       bonus: float, tide_score: float):
    """写入最终评分"""
    track = 'momentum' if tide_score >= 50 else 'reversion'
    label = '买入' if tide_score >= 60 else ('关注' if tide_score >= 40 else '观望')
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tide_score_signal 
            (trade_date, ts_code, l3_score, chanlun_bonus, tide_score, tide_track, tide_label)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            l3_score=VALUES(l3_score), chanlun_bonus=VALUES(chanlun_bonus),
            tide_score=VALUES(tide_score), tide_track=VALUES(tide_track),
            tide_label=VALUES(tide_label)
    """, (trade_date, ts_code, l3, bonus, tide_score, track, label))
    conn.commit()
    cur.close(); conn.close()


def run_scoring(trade_date: date = None) -> Dict:
    """全量评分入口"""
    from tide_engine.tide_chanlun_layer import apply_chanlun_layer

    if trade_date is None:
        trade_date = date.today()
    td_str = str(trade_date)

    codes = _get_watch_pool()
    weights = _get_base_weights()
    season = _get_season(td_str)
    total = len(codes)

    logger.info(f'[Tide] 开始评分: {total}只, season={season}, 权重={weights}')

    factor_fail = 0
    for i, code in enumerate(codes):
        try:
            factors = _compute_factors(code, td_str)
            l3 = _l3_score(factors, weights)
            _save_factor_value(td_str, code, factors, l3)
            # 缠论层
            cl = apply_chanlun_layer(code, td_str, factors, season)
            if cl['signals'].get('central') or cl['signals'].get('divergence') or cl['signals'].get('third_buy'):
                _save_chanlun_signal(td_str, code, cl['signals'])
            # 最终分
            tide_score = l3 + cl['bonus']
            tide_score = max(0, min(100, tide_score))
            _save_score_signal(td_str, code, l3, cl['bonus'], tide_score)
            if (i + 1) % 100 == 0:
                logger.info(f'[Tide] 进度 {i+1}/{total}')
        except Exception as e:
            factor_fail += 1
            logger.warning(f'[Tide] {code} 评分失败: {e}')
            continue

    logger.info(f'[Tide] 评分完成: {total-factor_fail}/{total}, 失败{factor_fail}')
    return {'total': total, 'success': total - factor_fail, 'fail': factor_fail,
            'trade_date': td_str, 'season': season}


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    run_scoring()
