"""
缠论批量分析器 — 对监控池全量股票做缠论分析并写入 chanlun_structure 表
供 score_pipeline.py 调用（独立文件，避免修改纯函数库）
"""
import math
import pymysql
from db_config import get_connection
from engine.chanlun_analyzer import analyze_chanlun


def analyze_pool_for_date(trade_date: str):
    """对监控池全量股票做缠论分析并逐只写入 chanlun_structure 表"""
    conn = get_connection()
    c = conn.cursor()

    c.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
    codes = [r['ts_code'] for r in c.fetchall()]

    done = 0
    for code in codes:
        try:
            c2 = conn.cursor()
            c2.execute("""SELECT trade_date, open, high, low, close 
                FROM daily_kline WHERE ts_code=%s AND trade_date<=%s
                ORDER BY trade_date ASC""", (code, trade_date))
            rows = c2.fetchall()
            if len(rows) < 30:
                done += 1
                continue
            ohlc = [{'high': float(r['high']), 'low': float(r['low']),
                     'close': float(r['close']),
                     'open': float(r['open']),
                     'trade_date': str(r['trade_date'])} for r in rows]

            result = analyze_chanlun(code, trade_date, ohlc)

            def safe(v, default=0):
                if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                    return default
                return v

            c2.execute("""INSERT INTO chanlun_structure 
                (ts_code, trade_date, zhongshu_count, structure_score, zoushi_type,
                 is_calculable, calc_error, bi_direction, zoushi_stage,
                 beichi_type, beichi_strength, beichi_validity,
                 buy_sell_point, autumn_tiger, tiger_confidence, tiger_reasons)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                 zhongshu_count=VALUES(zhongshu_count),
                 structure_score=VALUES(structure_score),
                 zoushi_type=VALUES(zoushi_type),
                 is_calculable=VALUES(is_calculable),
                 calc_error=VALUES(calc_error),
                 bi_direction=VALUES(bi_direction),
                 zoushi_stage=VALUES(zoushi_stage),
                 beichi_type=VALUES(beichi_type),
                 beichi_strength=VALUES(beichi_strength),
                 beichi_validity=VALUES(beichi_validity),
                 buy_sell_point=VALUES(buy_sell_point),
                 autumn_tiger=VALUES(autumn_tiger),
                 tiger_confidence=VALUES(tiger_confidence),
                 tiger_reasons=VALUES(tiger_reasons)""",
                (code, trade_date)
                + tuple(safe(result.get(k)) for k in
                    ['zhongshu_count', 'structure_score', 'zoushi_type',
                     'is_calculable', 'calc_error',
                     'bi_direction', 'zoushi_stage',
                     'beichi_type', 'beichi_strength', 'beichi_validity',
                     'buy_sell_point', 'autumn_tiger',
                     'tiger_confidence', 'tiger_reasons']))
            conn.commit()
            done += 1
        except Exception:
            pass

    conn.close()
    print(f'  缠论分析完成: {done}/{len(codes)}只 ✅')
