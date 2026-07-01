"""
板块特化权重映射 —— 优化3
=========================
19个板块的四维权重映射(trend/momentum/volatility/volume)
纯查表，零依赖。
"""

from typing import Dict

# BlockWeight = {'trend': float, 'momentum': float, 'volatility': float, 'volume': float}

BLOCK_WEIGHTS: Dict[str, Dict[str, float]] = {
    # 科技/AI类: 趋势+缠论权重大
    '半导体':   {'trend': 0.45, 'momentum': 0.30, 'volatility': 0.15, 'volume': 0.10},
    '元器件':   {'trend': 0.40, 'momentum': 0.30, 'volatility': 0.15, 'volume': 0.15},
    '通信设备': {'trend': 0.40, 'momentum': 0.35, 'volatility': 0.10, 'volume': 0.15},
    'IT设备':   {'trend': 0.45, 'momentum': 0.30, 'volatility': 0.10, 'volume': 0.15},
    '软件服务': {'trend': 0.35, 'momentum': 0.40, 'volatility': 0.10, 'volume': 0.15},
    # 消费类: 动量大
    '家用电器': {'trend': 0.30, 'momentum': 0.40, 'volatility': 0.15, 'volume': 0.15},
    '中成药':   {'trend': 0.30, 'momentum': 0.35, 'volatility': 0.15, 'volume': 0.20},
    '化学制药': {'trend': 0.30, 'momentum': 0.35, 'volatility': 0.15, 'volume': 0.20},
    '乳制品':   {'trend': 0.30, 'momentum': 0.40, 'volatility': 0.15, 'volume': 0.15},
    '批发业':   {'trend': 0.30, 'momentum': 0.35, 'volatility': 0.15, 'volume': 0.20},
    # 周期类: 均值回归权重大(波动因子上调)
    '化工原料': {'trend': 0.30, 'momentum': 0.30, 'volatility': 0.20, 'volume': 0.20},
    '电气设备': {'trend': 0.35, 'momentum': 0.30, 'volatility': 0.20, 'volume': 0.15},
    '专用机械': {'trend': 0.30, 'momentum': 0.30, 'volatility': 0.20, 'volume': 0.20},
    '玻璃':     {'trend': 0.25, 'momentum': 0.30, 'volatility': 0.25, 'volume': 0.20},
    '普钢':     {'trend': 0.25, 'momentum': 0.25, 'volatility': 0.25, 'volume': 0.25},
    '化工机械': {'trend': 0.30, 'momentum': 0.30, 'volatility': 0.20, 'volume': 0.20},
    '水力发电': {'trend': 0.30, 'momentum': 0.25, 'volatility': 0.20, 'volume': 0.25},
    '火力发电': {'trend': 0.30, 'momentum': 0.30, 'volatility': 0.20, 'volume': 0.20},
    '小金属':   {'trend': 0.25, 'momentum': 0.30, 'volatility': 0.25, 'volume': 0.20},


    '银行':     {'trend': 0.20, 'momentum': 0.20, 'volatility': 0.25, 'volume': 0.35},
    '证券':     {'trend': 0.35, 'momentum': 0.35, 'volatility': 0.15, 'volume': 0.15},
    '保险':     {'trend': 0.20, 'momentum': 0.25, 'volatility': 0.25, 'volume': 0.30},
    '多元金融': {'trend': 0.30, 'momentum': 0.30, 'volatility': 0.20, 'volume': 0.20},
    # 资源能源类: 均值回归
    '煤炭开采': {'trend': 0.25, 'momentum': 0.25, 'volatility': 0.25, 'volume': 0.25},
    '石油开采': {'trend': 0.25, 'momentum': 0.25, 'volatility': 0.25, 'volume': 0.25},
    '石油加工': {'trend': 0.25, 'momentum': 0.25, 'volatility': 0.25, 'volume': 0.25},
    '铝':       {'trend': 0.25, 'momentum': 0.25, 'volatility': 0.25, 'volume': 0.25},
    '铜':       {'trend': 0.25, 'momentum': 0.25, 'volatility': 0.25, 'volume': 0.25},
    '铅锌':     {'trend': 0.25, 'momentum': 0.25, 'volatility': 0.25, 'volume': 0.25},
    '黄金':     {'trend': 0.20, 'momentum': 0.25, 'volatility': 0.25, 'volume': 0.30},
    # 军工类: 趋势强、波动大
    '航空':     {'trend': 0.40, 'momentum': 0.35, 'volatility': 0.15, 'volume': 0.10},
    '船舶':     {'trend': 0.40, 'momentum': 0.30, 'volatility': 0.15, 'volume': 0.15},
    # 基建地产类: 趋势弱、波动大
    '建筑工程': {'trend': 0.25, 'momentum': 0.25, 'volatility': 0.30, 'volume': 0.20},
    '全国地产': {'trend': 0.20, 'momentum': 0.25, 'volatility': 0.30, 'volume': 0.25},
    '区域地产': {'trend': 0.20, 'momentum': 0.25, 'volatility': 0.30, 'volume': 0.25},
    '水泥':     {'trend': 0.25, 'momentum': 0.25, 'volatility': 0.25, 'volume': 0.25},
    # 交通物流类: 量能辅助大
    '港口':     {'trend': 0.25, 'momentum': 0.25, 'volatility': 0.20, 'volume': 0.30},
    '水运':     {'trend': 0.30, 'momentum': 0.25, 'volatility': 0.20, 'volume': 0.25},
    '仓储物流': {'trend': 0.30, 'momentum': 0.30, 'volatility': 0.20, 'volume': 0.20},
    '路桥':     {'trend': 0.20, 'momentum': 0.20, 'volatility': 0.25, 'volume': 0.35},
    # 汽车产业链
    '汽车整车': {'trend': 0.35, 'momentum': 0.35, 'volatility': 0.15, 'volume': 0.15},
    '汽车配件': {'trend': 0.30, 'momentum': 0.30, 'volatility': 0.20, 'volume': 0.20},
    # 消费制造
    '纺织':     {'trend': 0.25, 'momentum': 0.30, 'volatility': 0.20, 'volume': 0.25},
    '服饰':     {'trend': 0.25, 'momentum': 0.35, 'volatility': 0.20, 'volume': 0.20},
    '造纸':     {'trend': 0.25, 'momentum': 0.30, 'volatility': 0.20, 'volume': 0.25},
    '食品':     {'trend': 0.30, 'momentum': 0.35, 'volatility': 0.15, 'volume': 0.20},
    # 医药
    '生物制药': {'trend': 0.30, 'momentum': 0.35, 'volatility': 0.15, 'volume': 0.20},
    '医疗保健': {'trend': 0.25, 'momentum': 0.35, 'volatility': 0.20, 'volume': 0.20},
    # 农业
    '农业综合': {'trend': 0.25, 'momentum': 0.30, 'volatility': 0.25, 'volume': 0.20},
    '饲料':     {'trend': 0.25, 'momentum': 0.30, 'volatility': 0.20, 'volume': 0.25},
    '农药化肥': {'trend': 0.25, 'momentum': 0.30, 'volatility': 0.25, 'volume': 0.20},
    # 公用事业
    '环境保护': {'trend': 0.30, 'momentum': 0.25, 'volatility': 0.20, 'volume': 0.25},
    '水务':     {'trend': 0.25, 'momentum': 0.20, 'volatility': 0.25, 'volume': 0.30},
    '供气供热': {'trend': 0.25, 'momentum': 0.20, 'volatility': 0.25, 'volume': 0.30},
    '新型电力': {'trend': 0.30, 'momentum': 0.25, 'volatility': 0.20, 'volume': 0.25},
    # 科技类补充
    '互联网':   {'trend': 0.35, 'momentum': 0.40, 'volatility': 0.10, 'volume': 0.15},
    '广告包装': {'trend': 0.30, 'momentum': 0.35, 'volatility': 0.15, 'volume': 0.20},
    '影视音像': {'trend': 0.30, 'momentum': 0.35, 'volatility': 0.15, 'volume': 0.20},
}

_DEFAULT_WEIGHTS = {'trend': 0.35, 'momentum': 0.30, 'volatility': 0.20, 'volume': 0.15}


def get_block_weights(industry: str) -> Dict[str, float]:
    """查表返回四维权重；industry 为 None/空时返回默认权重"""
    if not industry:
        return dict(_DEFAULT_WEIGHTS)
    return dict(BLOCK_WEIGHTS.get(industry, _DEFAULT_WEIGHTS))


def adjust_weights_by_season(weights: Dict[str, float], season: str) -> Dict[str, float]:
    """
    根据当前季节动态调整板块权重
    - 春/夏 → 趋势/动量权重大 (追涨)
    - 秋/冬 → 波动/量能权重大 (抄底)
    - 混沌 → 保持原始
    """
    w = dict(weights)
    if season in ('spring', 'summer'):
        # 趋势+0.05 动量+0.05 波动-0.05 量能-0.05
        adj = 0.05
        w['trend'] = w.get('trend', 0.35) + adj
        w['momentum'] = w.get('momentum', 0.30) + adj
        w['volatility'] = max(0.05, w.get('volatility', 0.20) - adj)
        w['volume'] = max(0.05, w.get('volume', 0.15) - adj)
    elif season in ('autumn', 'winter'):
        # 波动+0.1 量能+0.05 趋势-0.1 动量-0.05
        w['volatility'] = w.get('volatility', 0.20) + 0.10
        w['volume'] = w.get('volume', 0.15) + 0.05
        w['trend'] = max(0.05, w.get('trend', 0.35) - 0.10)
        w['momentum'] = max(0.05, w.get('momentum', 0.30) - 0.05)
    # 归一化
    total = sum(w.values())
    if total > 0:
        for k in w:
            w[k] = round(w[k] / total, 3)
    return w


def apply_block_weights(
    trend: float, momentum: float, volatility: float, volume: float,
    weights: Dict[str, float],
) -> float:
    """用板块权重重新合成L2总分"""
    return (
        trend * weights['trend']
        + momentum * weights['momentum']
        + volatility * weights['volatility']
        + volume * weights['volume']
    )
