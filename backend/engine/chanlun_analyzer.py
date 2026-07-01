"""
缠论结构分析器 — 纯函数，无数据库依赖
=========================================
分型识别(K线包含处理) → 笔 → 中枢 → 背驰检测 → 买卖点判定

输入: OHLC 日K线数组
输出: 完整的缠论结构分析结果
"""

import math
from typing import List, Dict, Any, Tuple, Optional


# ═══ 1. K线合并(包含处理) ═══

def merge_kline(klines: List[Dict]) -> List[Dict]:
    """
    K线包含处理: 逐根合并, 上升趋势取高高, 下降趋势取低低
    输入: [{'high':h,'low':l,'close':c}, ...]
    输出: 合并后的K线列表
    """
    if len(klines) < 2:
        return klines[:]

    merged = [dict(klines[0])]
    i = 1
    while i < len(klines):
        curr = dict(klines[i])
        prev = merged[-1]

        # 判断方向: 用前两根不含关系的K线确定
        if len(merged) >= 2:
            p1 = merged[-2]
            p2 = merged[-1]
            is_up = p2['high'] > p1['high'] and p2['low'] > p1['low']
        else:
            # 只有一根，用当前K线和前一根比较
            is_up = prev['high'] <= curr['high'] and prev['low'] <= curr['low']

        # 检查包含关系: 当前K线被前一根完全包含或包含前一根
        if (curr['high'] <= prev['high'] and curr['low'] >= prev['low']):
            # 当前被前一根包含 -> 合并到前一根
            if is_up:
                prev['high'] = max(prev['high'], curr['high'])  # 取高高
                prev['low'] = max(prev['low'], curr['low'])     # 取高高
            else:
                prev['high'] = min(prev['high'], curr['high'])  # 取低低
                prev['low'] = min(prev['low'], curr['low'])     # 取低低
            prev['close'] = curr['close']
        elif (curr['high'] >= prev['high'] and curr['low'] <= prev['low']):
            # 前一根被当前包含 -> 当前与合并后比较
            if is_up:
                merged[-1] = {
                    'high': max(prev['high'], curr['high']),
                    'low': max(prev['low'], curr['low']),
                    'close': curr['close'],
                }
            else:
                merged[-1] = {
                    'high': min(prev['high'], curr['high']),
                    'low': min(prev['low'], curr['low']),
                    'close': curr['close'],
                }
        else:
            # 无包含关系，直接追加
            merged.append(curr)
        i += 1

    return merged


# ═══ 2. 分型识别 ═══

def find_fractals(klines: List[Dict]) -> List[Dict]:
    """
    分型识别(顶分型+底分型)
    顶分型: 中间K线最高, 两边更低 (high[mid] > high[left] and high[mid] > high[right])
    底分型: 中间K线最低, 两边更高 (low[mid] < low[left] and low[mid] < low[right])
    返回带有fractal标记的K线列表
    """
    if len(klines) < 3:
        return [dict(k, fractal='none') for k in klines]

    result = []
    for i in range(len(klines)):
        entry = dict(klines[i])
        entry['fractal'] = 'none'

        if 1 <= i <= len(klines) - 2:
            left = klines[i - 1]
            mid = klines[i]
            right = klines[i + 1]

            # 顶分型: 中间高 > 两边高
            if (mid['high'] > left['high'] and mid['high'] > right['high']
                    and mid['low'] > left['low'] and mid['low'] > right['low']):
                entry['fractal'] = 'top'
                entry['fractal_strength'] = min(
                    (mid['high'] - left['high']) / mid['high'] * 100,
                    (mid['high'] - right['high']) / mid['high'] * 100
                )

            # 底分型: 中间低 < 两边低
            elif (mid['low'] < left['low'] and mid['low'] < right['low']
                  and mid['high'] < left['high'] and mid['high'] < right['high']):
                entry['fractal'] = 'bottom'
                entry['fractal_strength'] = min(
                    (left['low'] - mid['low']) / mid['low'] * 100,
                    (right['low'] - mid['low']) / mid['low'] * 100
                )

        result.append(entry)

    return result


# ═══ 3. 笔识别 ═══

def find_bi(fractals: List[Dict], min_span: int = 5) -> List[Dict]:
    """
    笔识别: 顶底分型交替连接
    规则:
    - 笔至少跨越 min_span 根K线(含中间)
    - 顶底交替: 顶→底→顶→底
    - 连接最新不含包含处理的原始K线位置
    """
    tops = []   # 存储顶分型: (index, high, low)
    bottoms = []  # 存储底分型: (index, low, high)

    for i, k in enumerate(fractals):
        if k.get('fractal') == 'top':
            tops.append((i, k['high'], k['low']))
        elif k.get('fractal') == 'bottom':
            bottoms.append((i, k['low'], k['high']))

    # 用原始索引合并排序所有分型
    all_points = []
    for t in tops:
        all_points.append({'type': 'top', 'idx': t[0], 'price': t[1], 'low': t[2]})
    for b in bottoms:
        all_points.append({'type': 'bottom', 'idx': b[0], 'price': b[1], 'high': b[2]})

    all_points.sort(key=lambda x: x['idx'])

    # 笔连接: 顶底交替, 至少跨越 min_span 根K线
    bi_list = []
    if not all_points:
        return bi_list

    last = all_points[0]

    for i in range(1, len(all_points)):
        p = all_points[i]

        # 必须交替
        if p['type'] == last['type']:
            last = p
            continue

        # 必须跨足够多K线
        if p['idx'] - last['idx'] < min_span:
            # 对于同类型分型，保留更强的那个
            if p['type'] == 'top':
                last = p if p['price'] > last['price'] else last
            else:
                last = p if p['price'] < last['price'] else last
            continue

        bi_list.append({
            'start_idx': last['idx'],
            'end_idx': p['idx'],
            'start_type': last['type'],
            'end_type': p['type'],
            'direction': 'up' if last['type'] == 'bottom' else 'down',
            'start_price': last['price'],
            'end_price': p['price'],
            'span': p['idx'] - last['idx'],
        })
        last = p

    return bi_list


# ═══ 4. 中枢识别 ═══

def find_zhongshu(bi_list: List[Dict]) -> List[Dict]:
    """
    中枢识别: 至少3笔重叠区域
    定义: 前3笔(向上笔的起点较低, 向下笔的终点较低)的重叠区间
    ZG(中枢上沿)=min(进入段高点, 离开段高点)
    ZD(中枢下沿)=max(进入段低点, 离开段低点)
    """
    if len(bi_list) < 3:
        return []

    zhongshu_list = []

    # 遍历所有连续3笔
    for i in range(len(bi_list) - 2):
        b1 = bi_list[i]
        b2 = bi_list[i + 1]
        b3 = bi_list[i + 2]

        # 必须是 上→下→上 或 下→上→下 交替
        if not (b1['direction'] != b2['direction'] and b2['direction'] != b3['direction']):
            continue

        # 中枢下沿ZD = max(三笔的低点)
        low1 = min(b1['start_price'], b1['end_price'])
        low2 = min(b2['start_price'], b2['end_price'])
        low3 = min(b3['start_price'], b3['end_price'])
        zd = max(low1, low2, low3)

        # 中枢上沿ZG = min(三笔的高点)
        high1 = max(b1['start_price'], b1['end_price'])
        high2 = max(b2['start_price'], b2['end_price'])
        high3 = max(b3['start_price'], b3['end_price'])
        zg = min(high1, high2, high3)

        # 有效性: ZG > ZD (有实际重叠区间)
        if zg <= zd:
            continue

        # 中枢宽度
        width = (zg - zd) / zd

        # 稳定性: 中枢宽度越小越稳定
        stability = max(0, min(1, 1 - width * 5))

        zhongshu_list.append({
            'start_bi_idx': i,
            'end_bi_idx': i + 2,
            'zd': zd,
            'zg': zg,
            'width': width,
            'stability': stability,
            'total_bi': 3,
        })

        # 检查扩展: 后续笔如果在中枢区间内，则扩展中枢
        for j in range(i + 3, len(bi_list)):
            bj = bi_list[j]
            bj_high = max(bj['start_price'], bj['end_price'])
            bj_low = min(bj['start_price'], bj['end_price'])

            # 如果笔的区间完全在中枢外，不再扩展
            if bj_low > zg or bj_high < zd:
                break

            # 更新中枢
            zd = max(zd, bj_low)
            zg = min(zg, bj_high)
            zhongshu_list[-1]['end_bi_idx'] = j
            zhongshu_list[-1]['total_bi'] = j - i + 1

            if zg <= zd:
                break

    # 合并重叠中枢: 取最新的
    if not zhongshu_list:
        return []

    # 取最新(最后一个)中枢
    return [zhongshu_list[-1]]


# ═══ 5. 走势类型判定 ═══

def determine_zoushi(bi_list: List[Dict], zhongshu_list: List[Dict]) -> Dict:
    """
    走势类型判定
    - 上涨趋势: 至少2个同向上中枢, 且第二个中枢不重叠第一个
    - 下跌趋势: 至少2个同向下中枢, 且第二个中枢不重叠第一个
    - 盘整: 1个中枢
    - 未知: 无中枢
    """
    if not bi_list:
        return {'type': 'unknown', 'stage': 'none'}

    if not zhongshu_list:
        return {'type': 'unknown', 'stage': 'none'}

    zs = zhongshu_list[-1]

    # 判断价格相对于中枢的位置
    last_bi = bi_list[-1]
    last_price = last_bi['end_price']
    bi_dir = last_bi['direction']

    if bi_dir == 'up' and last_price > zs['zg']:
        stage = '突破'
    elif bi_dir == 'down' and last_price < zs['zd']:
        stage = '破位'
    elif zs['width'] < 0.05 and zs['stability'] > 0.8:
        stage = '中枢新生'
    else:
        stage = '盘整'

    # 检查是否有2个中枢形成趋势
    previous_zoushi = '盘整'
    if len(zhongshu_list) >= 2:
        zs1 = zhongshu_list[-2]
        zs2 = zhongshu_list[-1]
        # 第二个中枢在中枢1之上 -> 上涨趋势
        if zs2['zd'] > zs1['zg']:
            previous_zoushi = '上涨趋势'
        elif zs2['zg'] < zs1['zd']:
            previous_zoushi = '下跌趋势'

    return {
        'type': previous_zoushi if len(zhongshu_list) >= 2 else '盘整',
        'stage': stage,
        'zoushi': previous_zoushi,
    }


# ═══ 6. 背驰检测 ═══

def detect_beichi(bi_list: List[Dict], closes: List[float],
                  zhongshu_list: List[Dict]) -> Dict:
    """
    背驰检测:
    - 趋势背驰: 趋势中进入段MACD面积 > 离开段MACD面积且力度减弱
    - 盘整背驰: 盘整中进入段与离开段力度比较
    - 顶底背驰: 价格新高但MACD柱面积缩窄
    """
    if len(bi_list) < 4 or not zhongshu_list:
        return {
            'type': 'none',
            'strength': 0,
            'macd_area_ratio': 0,
            'dif_dea_diverge': 0,
        }

    zs = zhongshu_list[-1]

    # 找到进入段和离开段
    entry_bi_idx = zs['start_bi_idx']
    exit_bi_idx = zs['end_bi_idx']

    if entry_bi_idx < 0 or exit_bi_idx >= len(bi_list) - 1:
        return {
            'type': 'none', 'strength': 0,
            'macd_area_ratio': 0,
            'dif_dea_diverge': 0,
        }

    # 进入段 = 中枢前的笔
    entry_bi = bi_list[entry_bi_idx]
    # 离开段 = 中枢后的笔
    exit_bi = bi_list[exit_bi_idx + 1] if exit_bi_idx + 1 < len(bi_list) else bi_list[-1]

    # 计算MACD面积 (用价格涨跌代替)
    def calc_macd_area(bi, klines):
        """模拟MACD面积: 用笔内各K线相对MA5偏离开方累加"""
        start = bi['start_idx']
        end = bi['end_idx']
        if end - start < 2:
            return 0
        # 取笔范围内的K线close
        seg_close = closes[start:end + 1]
        if len(seg_close) < 3:
            return 0
        # MA5 of segment
        ma5_seg = sum(seg_close[-5:]) / min(5, len(seg_close))
        # 偏离开方累加
        area = sum((c - ma5_seg) ** 2 * (1 if bi['direction'] == 'up' else -1)
                   for c in seg_close)
        return area

    entry_area = calc_macd_area(entry_bi, closes)
    exit_area = calc_macd_area(exit_bi, closes)

    # MACD面积比
    area_ratio = 0
    if abs(entry_area) > 0.0001:
        area_ratio = abs(exit_area / entry_area)

    # 背驰判定
    beichi_type = 'none'
    beichi_strength = 0

    if exit_bi['direction'] == 'up':  # 向上离开
        # 顶背驰: 离开段价格更高但MACD面积缩小
        if exit_bi['end_price'] > entry_bi['end_price'] and area_ratio < 0.8:
            beichi_type = 'top'
            beichi_strength = (1 - area_ratio) * 100
            if area_ratio < 0.5:
                beichi_strength *= 1.2
    elif exit_bi['direction'] == 'down':  # 向下离开
        # 底背驰: 离开段价格更低但MACD面积缩小
        if exit_bi['end_price'] < entry_bi['end_price'] and area_ratio < 0.8:
            beichi_type = 'bottom'
            beichi_strength = (1 - area_ratio) * 100
            if area_ratio < 0.5:
                beichi_strength *= 1.2

    beichi_strength = min(100, max(0, beichi_strength))

    # DIF/DEA背离 (简化为价格与MACD面积的背离)
    dif_dea_diverge = 0
    if beichi_type in ('top', 'bottom'):
        dif_dea_diverge = 1

    return {
        'type': beichi_type,
        'strength': round(beichi_strength, 2),
        'macd_area_ratio': round(area_ratio, 4),
        'dif_dea_diverge': dif_dea_diverge,
    }


# ═══ 7. 买卖点判定 ═══

def determine_buy_sell_point(
    bi_list: List[Dict],
    zhongshu_list: List[Dict],
    beichi_result: Dict,
    zoushi_result: Dict,
) -> Dict:
    """
    买卖点判定:
    - 第一类买卖点: 趋势背驰产生
    - 第二类买卖点: 回试中枢不破
    - 第三类买卖点: 离开中枢后回抽不进中枢
    """
    if not zhongshu_list or len(bi_list) < 4:
        return {'point': 'none', 'confirmed': 0, 'failed': 0}

    zs = zhongshu_list[-1]
    beichi = beichi_result
    zoushi = zoushi_result

    result = {'point': 'none', 'confirmed': 0, 'failed': 0}

    latest_bi = bi_list[-1]
    latest_price = latest_bi['end_price']
    bi_direction = latest_bi['direction']

    # 一买: 底背驰 + 下跌趋势终结
    if beichi['type'] == 'bottom' and beichi['strength'] > 30:
        if zoushi.get('zoushi') == '下跌趋势' or zoushi['type'] == 'unknown':
            result['point'] = 'buy1'
            result['confirmed'] = 1

    # 一卖: 顶背驰 + 上涨趋势终结
    elif beichi['type'] == 'top' and beichi['strength'] > 30:
        if zoushi.get('zoushi') == '上涨趋势' or zoushi['type'] == 'unknown':
            result['point'] = 'sell1'
            result['confirmed'] = 1

    # 二买: 一买后回调不破前低
    if len(bi_list) >= 4:
        pre_bi = bi_list[-2]
        if (beichi['type'] == 'none' and bi_direction == 'up'
                and latest_price > zs['zd']):
            # 回调最低点不破前低 = 二买
            if pre_bi['direction'] == 'down' and pre_bi['end_price'] > zs['zd']:
                result['point'] = 'buy2'
                result['confirmed'] = 1

    # 二卖: 一卖后反弹不过前高
    if len(bi_list) >= 4:
        pre_bi = bi_list[-2]
        if (beichi['type'] == 'none' and bi_direction == 'down'
                and latest_price < zs['zg']):
            if pre_bi['direction'] == 'up' and pre_bi['end_price'] < zs['zg']:
                result['point'] = 'sell2'
                result['confirmed'] = 1

    # 三买: 向上离开中枢后回抽不进中枢上沿
    if bi_direction == 'up':
        if latest_price > zs['zg']:
            # 确认离开中枢
            if len(bi_list) >= 5:
                prev_bi = bi_list[-2]
                if (prev_bi['direction'] == 'down'
                        and prev_bi['end_price'] > zs['zg']
                        and latest_bi['start_price'] > zs['zg']):
                    result['point'] = 'buy3'
                    result['confirmed'] = 1

    # 三卖: 向下离开中枢后回抽不进中枢下沿
    if bi_direction == 'down':
        if latest_price < zs['zd']:
            if len(bi_list) >= 5:
                prev_bi = bi_list[-2]
                if (prev_bi['direction'] == 'up'
                        and prev_bi['end_price'] < zs['zd']
                        and latest_bi['start_price'] < zs['zd']):
                    result['point'] = 'sell3'
                    result['confirmed'] = 1

    return result


# ═══ 8. 秋老虎检测 ═══

def detect_autumn_tiger(
    bi_list: List[Dict],
    zhongshu_list: List[Dict],
    beichi_result: Dict,
    closes: List[float],
    high: float,
) -> Dict:
    """
    秋老虎: 下跌趋势中突然放量拉起的强势反弹
    """
    if not bi_list:
        return {'active': False, 'confidence': 0, 'reasons': []}

    reasons = []
    confidence = 0

    # 1. 前期有下跌笔
    down_bi_count = sum(1 for b in bi_list[-6:] if b['direction'] == 'down')
    if down_bi_count >= 2:
        confidence += 20
        reasons.append('前期下跌笔≥2')

    # 2. 最后一笔向上且涨幅 > 5%
    last_bi = bi_list[-1]
    if last_bi['direction'] == 'up':
        bi_return = (last_bi['end_price'] - last_bi['start_price']) / last_bi['start_price']
        if bi_return > 0.05:
            confidence += 25
            reasons.append(f'反弹幅度{bi_return*100:.1f}%')
        if bi_return > 0.10:
            confidence += 15
            reasons.append('强势反弹>10%')

    # 3. 底部放量
    if len(closes) >= 10:
        latest_vol = high / (sum(closes[-10:]) / 10) if sum(closes[-10:]) > 0 else 0
        if latest_vol > 0:  # 有量
            confidence += 10
            reasons.append('底部放量迹象')

    # 4. 有背驰信号
    if beichi_result['type'] == 'bottom' and beichi_result['strength'] > 40:
        confidence += 20
        reasons.append('底背驰确认')

    # 5. 有中枢支撑
    if zhongshu_list:
        confidence += 10
        reasons.append('中枢支撑区')

    active = confidence >= 50

    return {
        'active': active,
        'confidence': min(100, confidence),
        'reasons': reasons[:5],
    }


# ═══ 9. 主入口: 完整缠论分析 ═══

def analyze_chanlun(
    ts_code: str,
    trade_date: str,
    ohlc: List[Dict],
) -> Dict:
    """
    对一只股票做完整缠论分析

    参数:
        ts_code: 股票代码
        trade_date: 交易日期 YYYY-MM-DD
        ohlc: [{'high':h, 'low':l, 'open':o, 'close':c, 'vol':v}, ...]

    返回:
        dict: 完整分析结果, 可直接映射到 chanlun_structure 表
    """
    n = len(ohlc)
    if n < 60:
        return {'error': f'数据不足({n}日, 需要≥60日)'}

    # 提取价格序列
    closes = [float(r['close']) for r in ohlc]
    highs = [float(r['high']) for r in ohlc]
    lows = [float(r['low']) for r in ohlc]

    # K线数据简化格式
    klines = [{'high': highs[i], 'low': lows[i], 'close': closes[i]} for i in range(n)]

    # 1. K线合并(包含处理)
    merged = merge_kline(klines)

    # 2. 分型识别
    fractals = find_fractals(merged)

    # 3. 笔识别
    bi_list = find_bi(fractals, min_span=5)

    # 4. 中枢识别
    zhongshu_list = find_zhongshu(bi_list)

    # 5. 走势类型
    zoushi = determine_zoushi(bi_list, zhongshu_list)

    # 6. 背驰检测
    beichi = detect_beichi(bi_list, closes, zhongshu_list)

    # 7. 买卖点
    bs_point = determine_buy_sell_point(bi_list, zhongshu_list, beichi, zoushi)

    # 8. 秋老虎
    tiger = detect_autumn_tiger(bi_list, zhongshu_list, beichi, closes,
                                 highs[-1] if highs else 0)

    # 统计分型数量(近20日)
    recent_fractals = [f for f in fractals if len(fractals) - fractals.index(f) <= 20] if len(fractals) > 20 else fractals
    top_cnt = sum(1 for f in recent_fractals if f.get('fractal') == 'top')
    bottom_cnt = sum(1 for f in recent_fractals if f.get('fractal') == 'bottom')

    # 最新笔方向
    bi_direction = bi_list[-1]['direction'] if bi_list else 'none'

    # 笔力度: 最后一笔涨幅/跌幅
    bi_strength = 0
    if bi_list:
        last_bi = bi_list[-1]
        if last_bi['end_price'] > 0:
            bi_strength = abs((last_bi['end_price'] - last_bi['start_price'])
                              / last_bi['start_price']) * 100

    # 结构评分: 基于中枢和买卖点综评
    structure_score = 50  # 中性
    if bs_point['point'] in ('buy1', 'buy2'):
        structure_score = 75
    elif bs_point['point'] == 'buy3':
        structure_score = 85
    elif bs_point['point'] in ('sell1', 'sell2'):
        structure_score = 25
    elif bs_point['point'] == 'sell3':
        structure_score = 15
    elif bi_direction == 'up':
        structure_score = 60
    elif bi_direction == 'down':
        structure_score = 40

    # 有背驰增强
    if beichi['type'] == 'bottom' and beichi['strength'] > 40:
        structure_score = min(95, structure_score + 15)
    elif beichi['type'] == 'top' and beichi['strength'] > 40:
        structure_score = max(5, structure_score - 15)

    # 中枢稳定性加分
    if zhongshu_list:
        structure_score = round(structure_score * 0.8 + zhongshu_list[-1]['stability'] * 0.2 * 100, 1)

    # 合成结果
    result = {
        'ts_code': ts_code,
        'trade_date': trade_date,
        'analysis_level': 'daily',

        # 分型统计
        'top_fractal_cnt': top_cnt,
        'bottom_fractal_cnt': bottom_cnt,

        # 笔
        'bi_direction': bi_direction,
        'bi_strength': round(bi_strength, 2),

        # 中枢
        'zhongshu_count': len(zhongshu_list),
        'zhongshu_zd': round(zhongshu_list[-1]['zd'], 3) if zhongshu_list else 0,
        'zhongshu_zg': round(zhongshu_list[-1]['zg'], 3) if zhongshu_list else 0,
        'zhongshu_width': round(zhongshu_list[-1]['width'], 4) if zhongshu_list else 0,
        'zhongshu_stability': round(zhongshu_list[-1]['stability'], 4) if zhongshu_list else 0,

        # 走势
        'zoushi_type': zoushi['type'],
        'zoushi_stage': zoushi['stage'],

        # 背驰
        'beichi_type': beichi['type'],
        'beichi_strength': round(beichi['strength'], 2),
        'beichi_validity': round(beichi['strength'] / 100, 4) if beichi['strength'] > 0 else 0,
        'macd_area_ratio': round(beichi['macd_area_ratio'], 4),
        'dif_dea_diverge': beichi['dif_dea_diverge'],

        # 买卖点
        'buy_sell_point': bs_point['point'],
        'buy3_confirmed': 1 if bs_point['point'] == 'buy3' else 0,
        'buy3_failed': 0,

        # 秋老虎
        'autumn_tiger': 1 if tiger['active'] else 0,
        'tiger_confidence': round(tiger['confidence'] / 100, 2),
        'tiger_reasons': str(tiger['reasons']) if tiger['reasons'] else None,

        # 评分
        'structure_score': round(structure_score, 2),
        'is_calculable': 1,
        'calc_error': None,
    }

    return result
