"""
p6_scorer.py - P6 评分引擎（唯一评分源）
全新构建，从零实现评分逻辑
"""
import logging
from datetime import datetime, date
from db_config import db_cursor, DATA_ERROR_MARKER

logger = logging.getLogger('p6_scorer')


def run_scoring(trade_date=None, ts_code=None):
    """
    执行P6评分
    - 从 daily_kline 读取K线数据
    - 计算趋势分 + 动量分 + 结构分 + 情绪分
    - 写入 strategy_signal
    """
    if trade_date is None:
        trade_date = date.today()

    logger.info(f'[P6 Scorer] 开始评分 trade_date={trade_date}')

    # 1. 获取监控池股票（如果没有指定单只股票）
    if ts_code:
        stock_list = [(ts_code,)]
    else:
        with db_cursor(commit=False) as cur:
            cur.execute(
                "SELECT ts_code FROM watch_pool WHERE is_active=1"
            )
            stock_list = cur.fetchall()

    if not stock_list:
        logger.warning('[P6 Scorer] 监控池为空，跳过评分')
        return 0

    total = len(stock_list)
    scored = 0
    errors = 0

    for row in stock_list:
        code = row['ts_code']
        try:
            result = _score_single_stock(code, trade_date)
            if result:
                _save_score(code, trade_date, result)
                scored += 1
        except Exception as e:
            errors += 1
            logger.error(f'[P6 Scorer] {code} 评分失败: {e}')

    logger.info(f'[P6 Scorer] 完成: {scored}/{total} 成功, {errors} 错误')
    return scored


def _score_single_stock(ts_code, trade_date):
    """对单只股票计算评分"""
    with db_cursor(commit=False) as cur:
        # 获取最近60个交易日K线
        cur.execute("""
            SELECT trade_date, open, high, low, close, change_pct, vol, amount
            FROM daily_kline
            WHERE ts_code=%s AND trade_date<=%s
            ORDER BY trade_date DESC LIMIT 60
        """, [ts_code, trade_date])
        klines = cur.fetchall()

    if not klines or len(klines) < 5:
        logger.debug(f'[P6 Scorer] {ts_code} K线不足(={len(klines) if klines else 0}), 跳过')
        return None

    # 检查数据异常标记
    for k in klines[:5]:
        if any(v == DATA_ERROR_MARKER for v in [k['open'], k['high'], k['low'], k['close'], k['vol']]):
            logger.warning(f'[P6 Scorer] {ts_code} 包含-1异常数据')
            return None

    latest = klines[0]
    close = float(latest['close'])
    change_pct = float(latest['change_pct'])

    # ─── 趋势分 (0-30) ───────────────────────────────────
    # 基于20日/10日/5日均线关系
    _closes = [float(k['close']) for k in klines]
    _closes_rev = _closes[::-1]  # 时间正序

    ma5 = _ma(_closes_rev[-5:]) if len(_closes_rev) >= 5 else 0
    ma10 = _ma(_closes_rev[-10:]) if len(_closes_rev) >= 10 else 0
    ma20 = _ma(_closes_rev[-20:]) if len(_closes_rev) >= 20 else 0

    trend_score = 0
    if ma5 > 0 and ma10 > 0 and ma20 > 0:
        # 多头排列: MA5 > MA10 > MA20
        if ma5 > ma10 > ma20:
            trend_score = 30
        elif ma5 > ma10 and ma10 > ma20:
            trend_score = 25
        elif ma5 > ma10 or ma10 > ma20:
            trend_score = 20
        elif close > ma20:
            trend_score = 15
        else:
            trend_score = 10

    # ─── 动量分 (0-30) ──────────────────────────────────
    # 基于涨跌幅+成交量变化
    momentum_score = 0

    # 短期动量（5日）
    if len(klines) >= 5:
        change_5d = sum(float(k['change_pct']) for k in klines[:5])
        vol_5d = sum(float(k['vol']) for k in klines[:5])
        vol_ma5 = _ma([float(k['vol']) for k in klines[5:10]]) if len(klines) >= 10 else 0
        vol_ratio = vol_5d / vol_ma5 if vol_ma5 > 0 else 1

        # 涨跌幅贡献
        if change_5d > 15:
            momentum_score += 15
        elif change_5d > 10:
            momentum_score += 12
        elif change_5d > 5:
            momentum_score += 9
        elif change_5d > 0:
            momentum_score += 5
        elif change_5d > -5:
            momentum_score += 2

        # 成交量贡献
        if vol_ratio > 2.0:
            momentum_score += 15
        elif vol_ratio > 1.5:
            momentum_score += 12
        elif vol_ratio > 1.2:
            momentum_score += 8
        elif vol_ratio > 0.8:
            momentum_score += 5
        elif vol_ratio > 0.5:
            momentum_score += 2

    # ─── 结构分 (0-20) ──────────────────────────────────
    # 基于最近走势形态
    structure_score = 0

    if len(klines) >= 10:
        high_10d = max(float(k['high']) for k in klines[:10])
        low_10d = min(float(k['low']) for k in klines[:10])
        range_10d = high_10d - low_10d

        if range_10d > 0:
            # 在区间内的位置
            pos = (close - low_10d) / range_10d
            if pos > 0.8:  # 突破高位
                structure_score = 18
            elif pos > 0.6:
                structure_score = 14
            elif pos > 0.4:
                structure_score = 10
            elif pos > 0.2:
                structure_score = 6
            else:  # 低位
                structure_score = 4

        # 近期低点抬高加分
        if len(klines) >= 20:
            low_20d = min(float(k['low']) for k in klines[:20])
            low_10d_seg = min(float(k['low']) for k in klines[:10])
            if low_10d_seg > low_20d:
                structure_score += 2

    # ─── 情绪分 (0-20) ──────────────────────────────────
    # 基于换手率+涨跌幅+综合市场情绪
    emotion_score = 0

    # 当日涨跌幅贡献
    if abs(change_pct) < 30:  # 排除异常值
        if change_pct > 5:
            emotion_score += 8
        elif change_pct > 2:
            emotion_score += 5
        elif change_pct > 0:
            emotion_score += 3
        elif change_pct > -2:
            emotion_score += 1

    # 成交量放大贡献
    vol_current = float(latest['vol'])
    if len(klines) >= 11:
        _vols = [float(k['vol']) for k in klines[1:11]]
        vol_avg = _ma(_vols)
        if vol_avg > 0:
            vol_ratio_cur = vol_current / vol_avg
            if vol_ratio_cur > 2:
                emotion_score += 8
            elif vol_ratio_cur > 1.5:
                emotion_score += 5

    # 连续上涨贡献
    up_days = sum(1 for k in klines[:5] if float(k['change_pct']) > 0)
    if up_days >= 4:
        emotion_score += 4
    elif up_days >= 3:
        emotion_score += 2

    # ─── 季节和市场体制（从 season_state 读取） ────────────
    season = ''
    regime = ''
    position_pct = 0
    gate_triggered = 0
    autumn_tiger = 0
    tiger_confidence = 0
    try:
        with db_cursor(commit=False) as cur:
            cur.execute(
                "SELECT season, position_advice, raw_score, confidence FROM season_state "
                "WHERE index_code='MARKET' OR index_code LIKE '000%' "
                "ORDER BY trade_date DESC LIMIT 1"
            )
            ss = cur.fetchone()
            if ss:
                season = ss['season'] or ''
                regime = ss.get('position_advice', '')
                market_score = float(ss.get('raw_score', 0) or 0)
                market_confidence = float(ss.get('confidence', 0) or 0)
                
                # ── 安全闸门（Gate） ──
                if season in ('chaos', 'panic') and market_confidence < 0.6:
                    gate_triggered = 1
                elif season == 'winter' and market_score < 40:
                    gate_triggered = 1
                elif season in ('summer', 'spring') and market_score > 65:
                    gate_triggered = 0
                else:
                    gate_triggered = 0
                
                # ── 秋老虎检测 ──
                if season in ('autumn', 'chaos_autumn'):
                    if momentum_score > 20:
                        autumn_tiger = 1
                        tiger_confidence = min(round(momentum_score / 30 * 100, 2), 100)
                
                # ── 季节联动校正：情绪分季节加权 ──
                # 冬/恐慌期：情绪分压缩30%（市场情绪偏悲观，高情绪分不可信）
                if season in ('winter', 'panic'):
                    emotion_score = round(emotion_score * 0.7, 1)
                # 混沌/混沌秋：情绪分压缩15%（方向不明，情绪分半信）
                elif season in ('chaos', 'chaos_spring', 'chaos_autumn'):
                    emotion_score = round(emotion_score * 0.85, 1)
                # 春季：情绪分加2（市场情绪回暖，给一定溢价）
                elif season in ('spring',):
                    emotion_score = min(emotion_score + 2, 20)
                # 夏季/秋季：情绪分不做调整
    except:
        pass

    # ─── 综合评分 ────────────────────────────────────────
    composite_score = trend_score + momentum_score + structure_score + emotion_score
    
    # 冬/混沌期整体评分减5分（市场环境恶劣，评分打折）
    if season in ('winter', 'chaos', 'chaos_spring', 'chaos_autumn', 'panic'):
        composite_score = max(composite_score - 5, 0)
    
    composite_score = min(composite_score, 100)

    # ─── 信号判定 ────────────────────────────────────────
    signal_type, signal_label = _determine_signal(composite_score, season)

    # 仓位建议
    if signal_type == 'STRONG_BUY':
        position_pct = 30
    elif signal_type == 'BUY':
        position_pct = 20
    elif signal_type == 'CAUTIOUS_BUY':
        position_pct = 10
    else:
        position_pct = 0

    return {
        'composite_score': round(composite_score, 2),
        'raw_score': round(composite_score, 2),
        'trend_score': round(trend_score, 2),
        'momentum_score': round(momentum_score, 2),
        'structure_score': round(structure_score, 2),
        'emotion_score': round(emotion_score, 2),
        'signal_type': signal_type,
        'signal_label': signal_label,
        'direction': 'LONG',
        'season': season,
        'regime': regime,
        'position_pct': round(position_pct, 2),
        'gate_triggered': gate_triggered,
        'autumn_tiger': autumn_tiger,
        'tiger_confidence': tiger_confidence,
        'is_calculable': 1,
    }


def _save_score(ts_code, trade_date, result):
    """写入 strategy_signal"""
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO strategy_signal
                (ts_code, trade_date, composite_score, raw_score,
                 trend_score, momentum_score, structure_score, emotion_score,
                 signal_type, signal_label, direction,
                 season, regime, position_pct,
                 gate_triggered, autumn_tiger, tiger_confidence, is_calculable)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
                composite_score=VALUES(composite_score),
                raw_score=VALUES(raw_score),
                trend_score=VALUES(trend_score),
                momentum_score=VALUES(momentum_score),
                structure_score=VALUES(structure_score),
                emotion_score=VALUES(emotion_score),
                signal_type=VALUES(signal_type),
                signal_label=VALUES(signal_label),
                direction=VALUES(direction),
                season=VALUES(season),
                regime=VALUES(regime),
                position_pct=VALUES(position_pct),
                gate_triggered=VALUES(gate_triggered),
                autumn_tiger=VALUES(autumn_tiger),
                tiger_confidence=VALUES(tiger_confidence),
                is_calculable=VALUES(is_calculable)
        """, (
            ts_code, trade_date,
            result['composite_score'], result['raw_score'],
            result['trend_score'], result['momentum_score'],
            result['structure_score'], result['emotion_score'],
            result['signal_type'], result['signal_label'],
            result['direction'],
            result['season'], result['regime'],
            result['position_pct'],
            result['gate_triggered'], result['autumn_tiger'],
            result['tiger_confidence'], result['is_calculable'],
        ))


def _determine_signal(score, season=''):
    """按评分判定信号类型（含季节联动校正）"""
    thresholds = _get_thresholds(season)
    
    # 季节联动校正：冬/混沌期评分降一级
    effective_score = score
    if season in ('winter', 'chaos', 'chaos_spring', 'chaos_autumn', 'panic'):
        effective_score = max(score - 5, 0)
    # 情绪分季节校正：冬/混沌期情绪分打7折
    # （此处在底层计算中实现，上层只做信号降级）

    if effective_score >= thresholds['strong_buy']:
        return 'STRONG_BUY', '强烈买入'
    elif effective_score >= thresholds['buy']:
        return 'BUY', '买入'
    elif effective_score >= thresholds['cautious']:
        return 'CAUTIOUS_BUY', '谨慎买入'
    elif effective_score >= thresholds['hold']:
        return 'HOLD', '持有'
    else:
        return 'SELL', '卖出'


def _get_thresholds(season=''):
    """获取信号阈值，按季节联动（MAY炒股哲学核心）
    
    - 基础阈值: strong_buy=80, buy=75, cautious=40, hold=20
    - 季节联动: 不同季节的buy_threshold覆盖基础值
      夏季☀️45、春季🌺45、秋季🍂38、冬季❄️40、混沌🌪️48
    """
    defaults = {'strong_buy': 80, 'buy': 75, 'cautious': 40, 'hold': 20}
    try:
        with db_cursor(commit=False) as cur:
            # 读季节特定阈值
            season_key = f'{season}_buy_threshold' if season else None
            keys = ['strong_buy_threshold', 'buy_threshold',
                    'cautious_buy_threshold', 'hold_threshold']
            params = keys[:]
            if season_key:
                keys.insert(0, season_key)
                params = keys[:]
            
            placeholders = ','.join(['%s'] * len(keys))
            cur.execute(
                f"SELECT config_key, config_value FROM strategy_config "
                f"WHERE config_key IN ({placeholders})",
                params
            )
            rows = cur.fetchall()
            config = {r['config_key']: float(r['config_value']) for r in rows}
            
            result = {
                'strong_buy': config.get('strong_buy_threshold', 80),
                'buy': config.get('buy_threshold', 75),
                'cautious': config.get('cautious_buy_threshold', 40),
                'hold': config.get('hold_threshold', 20),
            }
            
            # 季节联动：季节特定阈值覆盖buy_threshold
            if season_key and season_key in config:
                result['buy'] = config[season_key]
                # 季节越难做(on buy→cautious差距缩小
                if season in ('chaos', 'panic', 'winter'):
                    result['cautious'] = max(result['buy'] - 10, 20)
            
            return result
    except:
        return defaults


def _ma(values):
    """简单移动平均"""
    if not values:
        return 0
    return sum(values) / len(values)
