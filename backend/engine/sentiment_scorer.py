"""
L3 情绪辅助评分 v2.0 — 升级版
==============================
市场宽度(15%) + 量能状态(15%) + RSI(20%) + 资金流向(25%) + 技术指标背离(25%)

资金流向: 大单净流入/特大单净流入
技术指标: MACD金叉死叉 + 布林带位置 + KDJ超买超卖
"""
from dataclasses import dataclass
from typing import Optional, Dict

@dataclass
class SentimentResult:
    score: float          # 0-100
    breadth: float        # 市场宽度
    vol_regime: str       # 'high' | 'normal' | 'low'
    rsi: float            # RSI值
    money_flow_score: float = 50.0   # 资金流派生评分
    technical_score: float = 50.0    # 技术指标派生评分
    components: Dict = None          # 各维度明细


def score_sentiment(
    breadth_ratio: float,
    vol_regime: str,
    rsi_val: float,
    recent_chg: float,
    # v2.0 新增参数
    money_flow_net: Optional[float] = None,        # 净流入额(万元)
    money_flow_lg: Optional[float] = None,          # 大单净流入
    money_flow_elg: Optional[float] = None,         # 特大单净流入
    tech_macd_bar: Optional[float] = None,          # MACD柱值
    tech_boll_pos: Optional[float] = None,          # 布林带位置: -1~1 (-1=下轨, 0=中轨, 1=上轨)
    tech_kdj_j: Optional[float] = None,             # KDJ的J值
) -> SentimentResult:
    """
    情绪评分 v2.0: 多维度综合
    权重: 市场宽度15% + 量能15% + RSI20% + 资金流向25% + 技术指标25%
    """
    # ═══ 原有维度 ═══

    # 1. 市场宽度 (15%)
    bd = 50.0
    if breadth_ratio > 0.80: bd -= 10
    elif breadth_ratio > 0.65: bd += 5
    elif breadth_ratio > 0.50: bd += 3
    elif breadth_ratio < 0.30: bd -= 15
    elif breadth_ratio < 0.40: bd -= 8

    # 2. 量能状态 (15%)
    vl = 50.0
    if vol_regime == 'high': vl -= 5
    elif vol_regime == 'low': vl += 3

    # 3. RSI (20%)
    rs = 50.0
    if rsi_val > 80: rs -= 5
    elif rsi_val > 70: rs -= 2
    elif rsi_val < 20: rs += 8
    elif rsi_val < 30: rs += 5
    elif 40 <= rsi_val <= 60: rs += 3

    # 单日波动调整
    if abs(recent_chg) > 0.05: rs -= 5

    # ═══ v2.0 新增维度 ═══

    # 4. 资金流向 (25%)
    mf = 50.0
    if money_flow_net is not None:
        # 净流入阈值: ±5000万
        if money_flow_net > 5000: mf += 15
        elif money_flow_net > 2000: mf += 8
        elif money_flow_net > 500: mf += 3
        elif money_flow_net < -5000: mf -= 15
        elif money_flow_net < -2000: mf -= 8
        elif money_flow_net < -500: mf -= 3

    if money_flow_elg is not None:
        # 特大单净流入
        if money_flow_elg > 2000: mf += 10
        elif money_flow_elg > 500: mf += 5
        elif money_flow_elg < -2000: mf -= 10
        elif money_flow_elg < -500: mf -= 5

    if money_flow_lg is not None:
        if money_flow_lg > 2000: mf += 5
        elif money_flow_lg < -2000: mf -= 5

    mf = max(0, min(100, mf))

    # 5. 技术指标 (25%)
    tc = 50.0
    if tech_macd_bar is not None:
        # MACD柱: 正=多头势能, 负=空头势能
        if tech_macd_bar > 0: tc += min(15, tech_macd_bar * 5)
        else: tc -= min(15, abs(tech_macd_bar) * 5)

    if tech_boll_pos is not None:
        # 布林位置: -1~1, -1=下轨(超卖), 1=上轨(超买)
        if tech_boll_pos < -0.8: tc += 10  # 下轨, 超卖反弹机会
        elif tech_boll_pos < -0.5: tc += 5
        elif tech_boll_pos > 0.8: tc -= 10  # 上轨, 超买回调风险
        elif tech_boll_pos > 0.5: tc -= 5

    if tech_kdj_j is not None:
        # KDJ: J>100超买, J<0超卖
        if tech_kdj_j > 100: tc -= 8
        elif tech_kdj_j > 80: tc -= 3
        elif tech_kdj_j < 0: tc += 8
        elif tech_kdj_j < 20: tc += 3

    tc = max(0, min(100, tc))

    # ═══ 合成(加权平均) ═══
    score = bd * 0.15 + vl * 0.15 + rs * 0.20 + mf * 0.25 + tc * 0.25
    score = round(max(0, min(100, score)), 1)

    return SentimentResult(
        score=score,
        breadth=round(breadth_ratio * 100, 1),
        vol_regime=vol_regime,
        rsi=round(rsi_val, 1),
        money_flow_score=round(mf, 1),
        technical_score=round(tc, 1),
        components={
            'breadth': round(bd, 1),
            'volume': round(vl, 1),
            'rsi': round(rs, 1),
            'money_flow': round(mf, 1),
            'technical': round(tc, 1),
        }
    )
