"""
season.py - 季节判定引擎
判断当前市场所处季节（spring/summer/autumn/winter/chaos）
"""
import logging
from datetime import date
from db_config import db_cursor

logger = logging.getLogger('season_engine')


def detect_season(trade_date=None):
    """
    判断市场季节状态
    基于沪深300指数最近走势判断
    """
    if trade_date is None:
        trade_date = date.today()

    logger.info(f'[Season] 判断季节 trade_date={trade_date}')

    # 用上证指数（000001.SH）或沪深300（000300.SH）判断
    index_codes = ['000300.SH', '000001.SH']

    for index_code in index_codes:
        try:
            with db_cursor(commit=False) as cur:
                cur.execute("""
                    SELECT trade_date, close, change_pct
                    FROM daily_kline
                    WHERE ts_code=%s AND trade_date<=%s
                    ORDER BY trade_date DESC LIMIT 60
                """, [index_code, trade_date])
                klines = cur.fetchall()

            if klines and len(klines) >= 20:
                return _analyze(klines, trade_date, index_code)
        except Exception as e:
            logger.warning(f'[Season] {index_code} 分析失败: {e}')

    # 无数据时的默认值
    return _default_season(trade_date)


def _analyze(klines, trade_date, index_code):
    """分析K线判断季节"""
    _closes = [float(k['close']) for k in klines]
    _closes_rev = _closes[::-1]  # 时间正序

    # 各周期均线
    ma10 = _ma(_closes_rev[-10:]) if len(_closes_rev) >= 10 else 0
    ma20 = _ma(_closes_rev[-20:]) if len(_closes_rev) >= 20 else 0
    ma30 = _ma(_closes_rev[-30:]) if len(_closes_rev) >= 30 else 0

    latest_close = _closes[0]

    # 涨跌幅统计
    change_20d = sum(float(k['change_pct']) for k in klines[:20])
    change_10d = sum(float(k['change_pct']) for k in klines[:10])
    change_5d = sum(float(k['change_pct']) for k in klines[:5])

    # 振幅
    high_20d = max(float(k['close']) for k in klines[:20]) if len(klines) >= 20 else 0
    low_20d = min(float(k['close']) for k in klines[:20]) if len(klines) >= 20 else 0
    range_20d = (high_20d - low_20d) / low_20d * 100 if low_20d > 0 else 0

    # ─── 季节判定 ────────────────────────────────────────
    # Summer（牛市）：均线多头排列，持续上涨
    if (latest_close > ma20 > ma30 and change_20d > 5 and
            change_10d > 2 and range_20d < 15):
        season = 'summer'
        raw_score = 80
        confidence = 0.75
        position_advice = '积极加仓'
    # Spring（温和上涨）：缓慢上涨
    elif (latest_close > ma20 and change_20d > 0 and
          change_10d > -2 and range_20d < 20):
        season = 'spring'
        raw_score = 65
        confidence = 0.65
        position_advice = '适度加仓'
    # Autumn（震荡/回调）：横盘或小幅下跌
    elif (abs(change_20d) < 8 and range_20d < 18):
        season = 'autumn'
        raw_score = 50
        confidence = 0.6
        position_advice = '控制仓位'
    # Winter（下跌）：持续下跌
    elif (change_20d < -5 and change_10d < -3):
        season = 'winter'
        raw_score = 30
        confidence = 0.7
        position_advice = '减仓规避'
    # Chaos（混沌）：剧烈波动，方向不明
    elif (range_20d > 18 or (change_20d > 0 and change_10d < -3)):
        season = 'chaos'
        raw_score = 40
        confidence = 0.5
        position_advice = '谨慎观望'
    else:
        season = 'chaos'
        raw_score = 45
        confidence = 0.5
        position_advice = '谨慎观望'

    # 恒纪元判定
    if season in ('summer', 'spring'):
        hengjiyuan_level = '恒纪元'
        if latest_close > ma20 > ma30:
            hengjiyuan_score = confidence * 100 + (raw_score / 100) * 20
        else:
            hengjiyuan_score = confidence * 80
    elif season in ('autumn',):
        hengjiyuan_level = '混沌纪元'
        hengjiyuan_score = 40
    else:
        hengjiyuan_level = '混沌纪元'
        hengjiyuan_score = 20

    confidence_mult = 0.5 + confidence * 0.5

    # 写入数据库
    try:
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO season_state
                    (index_code, trade_date, season, raw_score, confidence,
                     position_advice, hengjiyuan_level, hengjiyuan_score,
                     confidence_mult)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    season=VALUES(season), raw_score=VALUES(raw_score),
                    confidence=VALUES(confidence),
                    position_advice=VALUES(position_advice),
                    hengjiyuan_level=VALUES(hengjiyuan_level),
                    hengjiyuan_score=VALUES(hengjiyuan_score),
                    confidence_mult=VALUES(confidence_mult)
            """, (index_code, trade_date, season, raw_score, confidence,
                  position_advice, hengjiyuan_level, hengjiyuan_score,
                  confidence_mult))
    except Exception as e:
        logger.error(f'[Season] 写入失败: {e}')

    return {
        'index_code': index_code,
        'trade_date': trade_date,
        'season': season,
        'raw_score': raw_score,
        'confidence': confidence,
        'position_advice': position_advice,
        'hengjiyuan_level': hengjiyuan_level,
        'hengjiyuan_score': round(hengjiyuan_score, 2),
        'confidence_mult': round(confidence_mult, 2),
    }


def _default_season(trade_date):
    """无数据时的默认季节"""
    result = {
        'index_code': 'MARKET',
        'trade_date': trade_date,
        'season': 'chaos',
        'raw_score': 45,
        'confidence': 0.5,
        'position_advice': '谨慎观望',
        'hengjiyuan_level': '混沌纪元',
        'hengjiyuan_score': 20,
        'confidence_mult': 0.75,
    }
    try:
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO season_state
                    (index_code, trade_date, season, raw_score, confidence,
                     position_advice, hengjiyuan_level, hengjiyuan_score,
                     confidence_mult)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    season=VALUES(season), raw_score=VALUES(raw_score),
                    confidence=VALUES(confidence),
                    position_advice=VALUES(position_advice),
                    hengjiyuan_level=VALUES(hengjiyuan_level),
                    hengjiyuan_score=VALUES(hengjiyuan_score),
                    confidence_mult=VALUES(confidence_mult)
            """, ('MARKET', trade_date, result['season'], result['raw_score'],
                  result['confidence'], result['position_advice'],
                  result['hengjiyuan_level'], result['hengjiyuan_score'],
                  result['confidence_mult']))
    except Exception as e:
        pass
    return result


def _ma(values):
    if not values:
        return 0
    return sum(values) / len(values)
