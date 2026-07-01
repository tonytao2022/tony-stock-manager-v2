"""
fill_missing_kline.py - 补全回测池中缺失的K线数据
"""
import sys, os, time, logging
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_config import db_cursor
from datetime import date, datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('fill_kline')


def fill():
    """补全回测池中缺失K线的股票"""
    with db_cursor(commit=False) as cur:
        cur.execute("""
            SELECT bp.ts_code, bp.name FROM backtest_pool bp
            WHERE bp.is_active=1
              AND bp.ts_code NOT IN (SELECT DISTINCT ts_code FROM daily_kline)
              AND bp.ts_code NOT LIKE '000001.SH'
              AND bp.ts_code NOT LIKE '000300.SH'
        """)
        stocks = cur.fetchall()

    if not stocks:
        logger.info('全部股票已有K线，无需补充')
        return 0

    logger.info(f'需要补K线: {len(stocks)}只')

    # 从旧stock_db查这些股票的K线数据（如果有的话）
    with db_cursor(commit=False) as cur:
        cur.execute("USE stock_db_v2")
        cur.execute("""
            SELECT dk.ts_code, COUNT(*) as cnt, MIN(trade_date), MAX(trade_date)
            FROM daily_kline dk
            WHERE dk.ts_code IN (SELECT ts_code FROM stock_db_v2.backtest_pool WHERE is_active=1
              AND ts_code NOT IN (SELECT DISTINCT ts_code FROM stock_db_v2.daily_kline))
            GROUP BY dk.ts_code
        """)
        old_rows = cur.fetchall()

    logger.info(f'旧stock_db中有K线数据的: {len(old_rows)}只')

    # 从旧stock_db复制
    if old_rows:
        with db_cursor() as cur:
            for row in old_rows:
                cur.execute("USE stock_db_v2")
                cur.execute("""
                    INSERT IGNORE INTO stock_db_v2.daily_kline
                    SELECT * FROM daily_kline WHERE ts_code=%s
                """, [row['ts_code']])
    # 重新检查
    with db_cursor(commit=False) as cur:
        cur.execute("""
            SELECT COUNT(*) FROM backtest_pool bp
            WHERE bp.is_active=1 AND bp.ts_code NOT IN (SELECT DISTINCT ts_code FROM daily_kline)
              AND bp.ts_code NOT LIKE '000001.SH' AND bp.ts_code NOT LIKE '000300.SH'
        """)
        still_missing = cur.fetchone()['COUNT(*)']

    logger.info(f'补全后仍缺失: {still_missing}只')
    return len(stocks)


if __name__ == '__main__':
    fill()
