"""
chanlun.py - 缠论分析引擎
"""
import logging
from db_config import db_cursor

logger = logging.getLogger('chanlun_engine')


def analyze_stock(ts_code, lookback=60):
    """对单只股票做缠论结构分析"""
    with db_cursor(commit=False) as cur:
        cur.execute("""
            SELECT trade_date, open, high, low, close, change_pct, vol
            FROM daily_kline
            WHERE ts_code=%s
            ORDER BY trade_date DESC LIMIT %s
        """, [ts_code, lookback])
        klines = cur.fetchall()

    if not klines or len(klines) < 10:
        return None

    klines = klines[::-1]  # 时间正序

    # 提取价格序列
    highs = [float(k['high']) for k in klines]
    lows = [float(k['low']) for k in klines]
    closes = [float(k['close']) for k in klines]

    # 趋势判定
    trend_type, score = _detect_trend(closes)

    # 阶段判定
    phase = _detect_phase(highs, lows, closes, trend_type)

    # 置信度
    confidence = _calc_confidence(klines, trend_type)

    details = {
        'high_low_ratio': round(max(highs[-20:]) / min(lows[-20:]), 4) if min(lows[-20:]) > 0 else 0,
        'volatility': round(_volatility(closes[-20:]), 2),
        'up_days': sum(1 for k in klines[-20:] if float(k['change_pct']) > 0),
        'down_days': sum(1 for k in klines[-20:] if float(k['change_pct']) < 0),
    }

    # 写入数据库
    try:
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO chanlun_structure
                    (ts_code, trade_date, trend_type, structure_score,
                     phase, confidence, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    trend_type=VALUES(trend_type),
                    structure_score=VALUES(structure_score),
                    phase=VALUES(phase),
                    confidence=VALUES(confidence),
                    details=VALUES(details)
            """, (ts_code, str(klines[-1]['trade_date']), trend_type,
                  score, phase, confidence,
                  str(details).replace("'", '"')))
    except Exception as e:
        logger.error(f'[Chanlun] {ts_code} 写入失败: {e}')

    return {
        'ts_code': ts_code,
        'trend_type': trend_type,
        'structure_score': score,
        'phase': phase,
        'confidence': confidence,
        'details': details,
    }


def analyze_all():
    """分析所有监控池股票"""
    with db_cursor(commit=False) as cur:
        cur.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
        stocks = cur.fetchall()

    results = []
    for s in stocks:
        try:
            r = analyze_stock(s['ts_code'])
            if r:
                results.append(r)
        except Exception as e:
            logger.error(f'[Chanlun] {s["ts_code"]} 分析失败: {e}')

    return results


def _detect_trend(closes):
    """判断趋势方向"""
    if len(closes) < 20:
        return 'sideways', 0

    # 各周期均线
    n = len(closes)
    ma5 = sum(closes[-5:]) / 5 if n >= 5 else 0
    ma10 = sum(closes[-10:]) / 10 if n >= 10 else 0
    ma20 = sum(closes[-20:]) / 20 if n >= 20 else 0

    latest = closes[-1]
    score = 0

    # 多头排列
    if ma5 > ma10 > ma20:
        score = 85
        return 'up', score
    # ma5 > ma20
    elif ma5 > ma20 and latest > ma20:
        score = 70
        return 'up', score
    # 空头排列
    elif ma5 < ma10 < ma20:
        score = 20
        return 'down', score
    # ma5 < ma20
    elif ma5 < ma20 and latest < ma20:
        score = 30
        return 'down', score
    else:
        score = 50
        return 'sideways', score


def _detect_phase(highs, lows, closes, trend_type):
    """判定缠论阶段"""
    if trend_type == 'up':
        # 上涨阶段
        high_10 = max(closes[-10:]) if len(closes) >= 10 else closes[-1]
        high_5 = max(closes[-5:]) if len(closes) >= 5 else closes[-1]
        if closes[-1] >= high_5 and closes[-1] > closes[-2]:
            return 'b3-离开'
        elif closes[-1] >= high_5:
            return 'b2-中枢'
        else:
            return 'b1-盘整'
    elif trend_type == 'down':
        low_10 = min(closes[-10:]) if len(closes) >= 10 else closes[-1]
        if closes[-1] <= low_10:
            return 's3-探底'
        else:
            return 's2-下跌中枢'
    else:
        return 'p1-盘整'


def _calc_confidence(klines, trend_type):
    """计算置信度"""
    if len(klines) < 20:
        return 0.3

    _closes = [float(k['close']) for k in klines[-20:]]
    _changes = [float(k['change_pct']) for k in klines[-20:]]

    # 趋势一致性
    up_count = sum(1 for c in _changes if c > 0)
    down_count = sum(1 for c in _changes if c < 0)
    consistency = abs(up_count - down_count) / 20

    # 波动率
    vol = _volatility(_closes)

    # 综合置信度
    confidence = consistency * 0.6 + min(vol / 5, 1) * 0.4
    return round(min(confidence, 1), 2)


def _volatility(values):
    """计算波动率"""
    if len(values) < 2:
        return 0
    mean = sum(values) / len(values)
    if mean == 0:
        return 0
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return (variance ** 0.5) / mean * 100
