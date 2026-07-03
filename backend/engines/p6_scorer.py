#!/usr/bin/env python3
"""
p6_scorer.py - 评分管道入口（被 daily_orch.py _step_score 调用）

直接调用 p6_dual_track_engine.daily_pipeline()
提供简单的 run_scoring(trade_date) 接口
"""
import os, sys, logging
from datetime import date, datetime

logger = logging.getLogger('p6_scorer')


def run_scoring(trade_date=None):
    """
    执行评分管道（直接调用原版p6_dual_track.daily_pipeline）

    Args:
        trade_date: date对象或None（自动取最新交易日）
    """
    backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)

    from p6_dual_track_engine import daily_pipeline

    logger.info(f'[P6Scorer] 开始评分 trade_date={trade_date or "auto"}')

    results = daily_pipeline(mode='watch_pool')

    saved = sum(1 for r in results if not r.get('_filtered', False))
    logger.info(f'[P6Scorer] 评分完成: {len(results)}只投喂, {saved}只入库')

    return results


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(name)s %(message)s')
    run_scoring()
