#!/usr/bin/env python3
"""
综合评分引擎 v4.0 — 五优化强化版
===================================
优化1: L1周期阶段增强 — 引入板块周期特征(行业指数偏离)
优化2: 缠论维度补回 — 从chanlun_structure读取（空时用多周期MA背离代理）
优化3: 板块特化权重 — 科技趋势40%/消费动量35%/周期均值回归30%
优化4: ATR动态止损 — 波动率自适应止损线(-1.5×ATR)
优化5: reversion触发收窄 — 严格仅秋冬/熊市启用(夏季/混沌=momentum)

公式:
  综合评分 = 周期阶段(30%) + 缠论结构(40%) + 情绪辅助(30%)
  仓位 = 基础仓位 × 缠论修正 × 恒纪元置信
  止损 = max(-5%, -1.5×ATR)
"""

import sys, os, math, pymysql
from db_config import get_connection
from datetime import date, datetime
from typing import Dict, List, Tuple, Optional
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ═══ 指标 ═══
def sma(d,p):
    if len(d)<p: return sum(d)/len(d) if d else 0
    return sum(d[-p:])/p

def rsi(c,p=14):
    if len(c)<p+1: return 50
    g=sum(max(0,c[i]-c[i-1]) for i in range(-p,0))
    l=sum(max(0,c[i-1]-c[i]) for i in range(-p,0))+0.0001
    return 100-100/(1+g/l)

def roc(c,p):
    if len(c)<=p: return 0
    return (c[-1]-c[-p-1])/c[-p-1]

def stddev(d,p):
    if len(d)<p: return 0
    avg=sum(d[-p:])/p
    return (sum((x-avg)**2 for x in d[-p:])/p)**0.5

def atr(highs,lows,closes,p=14):
    if len(closes)<p+1: return 0
    tr=[max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(-p,0)]
    return sum(tr)/p

# ═══ 优化1: L1周期阶段增强 ═══
def score_cycle_enhanced(season, regime, market_score, industry, closes):
    """
    v4.0 增强: 
    - 基础季节打分(季节×regime)
    - 板块周期特征: 行业指数相对位置(价格在年线位置)
    - 个股周期特征: 价格在120日均线位置
    """
    base = {'spring':75,'summer':60,'autumn':30,'winter':15,'panic':10,'recovery':60}.get(season,40)
    if regime=='bull': base=min(90,base+10)
    elif regime=='bear': base=max(10,base-10)
    base+=max(-10,min(10,market_score*2))

    # 板块周期特征: 用价格在年线/半年线位置衡量
    if len(closes)>=120:
        ma120=sma(closes,120); close=closes[-1]
        pos120=(close-ma120)/ma120 if ma120>0 else 0
        # 在MA120上方=强势期, 下方=弱势期
        if pos120>0.15: sector_boost=10
        elif pos120>0.05: sector_boost=5
        elif pos120<0: sector_boost=-5
        elif pos120<-0.1: sector_boost=-10
        else: sector_boost=0

        if len(closes)>=250:
            ma250=sma(closes,250)
            pos250=(close-ma250)/ma250 if ma250>0 else 0
            if pos250>0.1: sector_boost+=5
            elif pos250<0: sector_boost-=5
    else:
        sector_boost=0

    base=max(0,min(100,base+sector_boost))

    # 策略判定: 优化5 — reversion严格只在秋冬/熊市
    strategy='momentum'
    if season in ('autumn','winter','panic') or regime=='bear' or market_score<-3:
        strategy='reversion'

    return {'score':round(base,1),'strategy':strategy,'sector_boost':sector_boost}

# ═══ 优化2: 缠论维度补回 ═══
def score_chanlun_enhanced(rows, season, industry, ts_code=None):
    """
    v4.0 缠论增强:
    - 优先从数据库读取chanlun_structure(buy_sell_point/structure_score/beichi)
    - 数据为空时用多周期背离代理: MACD背离 + RSI背离 + MA乖离
    """
    # ── 从chanlun_structure表读取真实缠论数据 ──
    if ts_code:
        try:
            cur=get_connection().cursor()
            cur.execute(
                "SELECT buy_sell_point, structure_score, beichi_type, beichi_strength, "
                "zoushi_type, zoushi_stage, autumn_tiger, tiger_confidence, "
                "bi_direction, bi_strength, zhongshu_count, zhongshu_stability "
                "FROM chanlun_structure WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1",
                (ts_code,)
            )
            cl_row=cur.fetchone()
            cur.close()
            if cl_row and cl_row.get('structure_score') is not None:
                ss=float(cl_row['structure_score'])
                bs=cl_row['buy_sell_point'] or 'none'
                bt=cl_row['beichi_type'] or 'none'
                bstr=float(cl_row.get('beichi_strength',0) or 0)
                zt=cl_row['zoushi_type'] or 'unknown'
                at=cl_row['autumn_tiger'] or 0
                
                # 用结构评分映射到趋势/动量/波动/量能子维度
                base_cl=50.0
                if ss>=75: base_cl=80
                elif ss>=60: base_cl=65
                elif ss>=40: base_cl=50
                else: base_cl=30
                
                # 买卖点修正
                bs_boost=0
                if bs=='buy3': bs_boost=20
                elif bs=='buy2': bs_boost=10
                elif bs=='buy1': bs_boost=5
                elif bs=='sell3': bs_boost=-20
                elif bs=='sell2': bs_boost=-10
                elif bs=='sell1': bs_boost=-5
                
                # 背驰修正
                beichi_boost=0
                if bt=='bottom' and bstr>40: beichi_boost=15
                elif bt=='top' and bstr>40: beichi_boost=-15
                
                # 走势类型修正
                zoushi_boost=0
                if zt=='盘整' and bs in ('buy2','buy3'): zoushi_boost=10
                elif zt=='unknown': zoushi_boost=-5
                
                # 秋老虎加分
                tiger_boost=15 if at else 0
                
                chanlun_signal=bs_boost+beichi_boost+zoushi_boost+tiger_boost
                chanlun_signal=max(-100, min(100, chanlun_signal))
                
                return {
                    'total':round(max(0,min(100,base_cl+chanlun_signal*0.3)),1),
                    'trend':round(max(0,min(100,base_cl-10+chanlun_signal*0.2)),1),
                    'momentum':round(max(0,min(100,base_cl+bs_boost*0.5+tiger_boost*0.3)),1),
                    'volatility':round(max(0,min(100,base_cl-20+abs(chanlun_signal)*0.2)),1),
                    'volume':round(max(0,min(100,50+tiger_boost*0.3)),1),
                    'chanlun_signal':chanlun_signal,
                }
        except Exception:
            pass  # DB查询失败则fallback到代理算法
    
    closes=[float(r['close']) for r in rows]
    highs=[float(r['high']) for r in rows]
    lows=[float(r['low']) for r in rows]
    vols=[float(r.get('vol',0) or 0) for r in rows]
    n=len(closes)

    if n<120:
        return {'total':50,'trend':50,'momentum':50,'volatility':50,'volume':50,'chanlun_signal':0}

    close=closes[-1]

    # ── 趋势(40%) ──
    ma5=sma(closes,5); ma10=sma(closes,10); ma20=sma(closes,20)
    ma60=sma(closes,60); ma120=sma(closes,120)

    tr=0
    if ma5>ma10: tr+=8
    if ma5>ma20: tr+=7
    if ma10>ma20: tr+=10
    if ma20>ma60: tr+=10
    if ma20>ma120: tr+=5
    if close>ma5: tr+=5
    if close>ma20: tr+=5
    old_ma20=sma(closes[:-20],20) if n>80 else ma20
    slope20=(ma20-old_ma20)/old_ma20 if old_ma20>0 else 0
    tr+=max(0,min(25,(slope20+0.05)*250))
    yh=max(closes[-250:]) if n>=250 else max(closes)
    yl=min(closes[-250:]) if n>=250 else min(closes)
    if yh>yl: tr+=(close-yl)/(yh-yl)*25
    trend_score=round(max(0,min(100,tr)),1)

    # ── 动量(35%) ──
    r5=roc(closes,5); r10=roc(closes,10); r20=roc(closes,20); r14=rsi(closes,14)
    mo=0
    mo+=max(0,min(25,12.5+r5*50))
    mo+=max(0,min(20,10+r10*30))
    mo+=max(0,min(15,7.5+r20*20))
    mo+=max(0,min(20,r14*0.2))
    acc=r5-r20
    if acc>0.02: mo+=10
    elif acc>0: mo+=5
    if n>=6:
        up_vol=sum(1 for i in range(-5,0) if closes[i]>closes[i-1] and vols[i]>vols[i-1])
        mo+=up_vol*2
    momentum_score=round(max(0,min(100,mo)),1)

    # ── 波动(15%, 反转版) — 连续函数+滚动标准化 ──
    vol20=stddev(closes,20); vol60=stddev(closes,60)
    daily_vol=vol20/close if close>0 else 0.02
    # 波动率Z-score（相对自身60日历史的位置）
    vol_zscore = 0
    if vol60>0 and n>=60:
        vol_mean = sum(stddev(closes[i-20:i],20)/close for i in range(-60,0) if len(closes[i-20:i])==20) / 60
        vol_std = (sum((stddev(closes[i-20:i],20)/close - vol_mean)**2 for i in range(-60,0) if len(closes[i-20:i])==20) / 60)**0.5
        if vol_std > 0:
            vol_zscore = (daily_vol - vol_mean) / vol_std
    # 连续映射: 低波动=高分(低波异象), 高波动=低分
    vl = 50 - vol_zscore * 8  # 每1个标准差±8分
    vl = max(10, min(90, vl))
    # 滚动相对位置修正
    if vol60>0:
        vr=vol20/vol60
        if vr<0.7: vl+=8
        elif vr<0.85: vl+=4
        elif vr>1.5: vl-=8
        elif vr>1.2: vl-=4
    if n>=20:
        h20=max(closes[-20:]); mdd=(h20-close)/h20
        if mdd>0.15: vl+=6
        elif mdd>0.10: vl+=3
    volatility_score=round(max(10,min(90,vl)),1)

    # ── 量能(10%) ──
    v20m=sma(vols,20); v60m=sma(vols,60)
    vr_day=vols[-1]/v20m if v20m>0 else 1
    vo=50
    if v60m>0:
        vt=v20m/v60m
        if vt>1.3: vo-=8
        elif vt>1.1: vo-=3
        elif vt<0.7: vo+=5
        elif vt<0.9: vo+=3
    if vr_day>2.0: vo-=10
    elif vr_day>1.5: vo-=5
    elif 0.7<=vr_day<=1.3: vo+=3
    elif vr_day<0.5: vo+=5
    if n>=6:
        dn_vol=sum(1 for i in range(-5,0) if closes[i]<closes[i-1] and vols[i]>vols[i-1])
        vo-=dn_vol*3
    volume_score=round(max(0,min(100,vo)),1)

    # ── 缠论代理: 多周期背离检测 ──
    chanlun_signal=0  # -100~+100: 负=超跌反弹窗口, 正=趋势延续

    # MACD金叉/死叉 (12/26/9)
    ema12=sma(closes,12); ema26=sma(closes,26)
    # 简化: 判断MACD柱状图趋势
    if n>=35:
        old_ema12=sma(closes[-9:-1],12) if n>=38 else ema12
        if ema12>ema26 and old_ema12<=sma(closes[-9:-1],26) if n>=38 else False:
            chanlun_signal+=15  # 金叉

    # RSI背离: 价格创新高但RSI未创新高=顶背离
    if n>=40:
        h20_p=max(closes[-30:-10]); r20_p=rsi(closes[-30:-10],14)
        h20_n=max(closes[-10:]); r20_n=rsi(closes[-10:],14)
        if h20_n>h20_p and r20_n<r20_p-5: chanlun_signal-=20  # 顶背离
        l20_p=min(closes[-30:-10]); r20_p2=rsi(closes[-30:-10],14)
        l20_n=min(closes[-10:]); r20_n2=rsi(closes[-10:],14)
        if l20_n<l20_p and r20_n2>r20_p2+5: chanlun_signal+=20  # 底背离

    # MA乖离: 价格远离MA20=超跌/超涨
    if close>0 and n>=20:
        ma20_dev=(close-ma20)/ma20
        if ma20_dev<-0.1: chanlun_signal+=15  # 深度超跌
        elif ma20_dev<-0.05: chanlun_signal+=8
        elif ma20_dev>0.1: chanlun_signal-=10  # 追高危险

    # 连续K线方向
    if n>=5:
        cons_up=sum(1 for i in range(-4,0) if closes[i]>closes[i-1])
        cons_dn=sum(1 for i in range(-4,0) if closes[i]<closes[i-1])
        if cons_up>=4: chanlun_signal+=10
        elif cons_dn>=4: chanlun_signal-=5

    chanlun_signal=max(-100,min(100,chanlun_signal))

    # 合成
    total = trend_score*0.40 + momentum_score*0.35 + volatility_score*0.15 + volume_score*0.10
    # 缠论信号修正: ±15分
    total += chanlun_signal * 0.15

    return {
        'total':round(max(0,min(100,total)),1),
        'trend':trend_score,'momentum':momentum_score,
        'volatility':volatility_score,'volume':volume_score,
        'chanlun_signal':chanlun_signal
    }

# ═══ 优化3: 板块特化权重 ═══
BLOCK_WEIGHTS = {
    # 科技/AI类: 趋势+缠论权重大
    '半导体': {'trend':0.45,'momentum':0.30,'volatility':0.15,'volume':0.10},
    '元器件': {'trend':0.40,'momentum':0.30,'volatility':0.15,'volume':0.15},
    '通信设备': {'trend':0.40,'momentum':0.35,'volatility':0.10,'volume':0.15},
    'IT设备': {'trend':0.45,'momentum':0.30,'volatility':0.10,'volume':0.15},
    '软件服务': {'trend':0.35,'momentum':0.40,'volatility':0.10,'volume':0.15},
    # 消费类: 动量大
    '家用电器': {'trend':0.30,'momentum':0.40,'volatility':0.15,'volume':0.15},
    '中成药': {'trend':0.30,'momentum':0.35,'volatility':0.15,'volume':0.20},
    '化学制药': {'trend':0.30,'momentum':0.35,'volatility':0.15,'volume':0.20},
    '乳制品': {'trend':0.30,'momentum':0.40,'volatility':0.15,'volume':0.15},
    '批发业': {'trend':0.30,'momentum':0.35,'volatility':0.15,'volume':0.20},
    # 周期类: 均值回归权重大(波动因子上调)
    '化工原料': {'trend':0.30,'momentum':0.30,'volatility':0.20,'volume':0.20},
    '电气设备': {'trend':0.35,'momentum':0.30,'volatility':0.20,'volume':0.15},
    '专用机械': {'trend':0.30,'momentum':0.30,'volatility':0.20,'volume':0.20},
    '玻璃': {'trend':0.25,'momentum':0.30,'volatility':0.25,'volume':0.20},
    '普钢': {'trend':0.25,'momentum':0.25,'volatility':0.25,'volume':0.25},
    '化工机械': {'trend':0.30,'momentum':0.30,'volatility':0.20,'volume':0.20},
    '水力发电': {'trend':0.30,'momentum':0.25,'volatility':0.20,'volume':0.25},
    '火力发电': {'trend':0.30,'momentum':0.30,'volatility':0.20,'volume':0.20},
    '小金属': {'trend':0.25,'momentum':0.30,'volatility':0.25,'volume':0.20},
}

def get_block_weights(industry):
    if not industry: return {'trend':0.35,'momentum':0.30,'volatility':0.20,'volume':0.15}
    return BLOCK_WEIGHTS.get(industry, {'trend':0.35,'momentum':0.30,'volatility':0.20,'volume':0.15})

# ═══ 优化4: ATR动态止损 ═══
def calc_stop_loss(closes, highs, lows, position_pct, strategy):
    """基于ATR的动态止损线"""
    n=len(closes)
    if n<20: return -0.05
    atr14=atr(highs,lows,closes,14)
    close=closes[-1]
    if close<=0: return -0.05

    atr_pct=atr14/close

    # 基准: -1.5×ATR
    stop=-atr_pct*1.5

    # momentum策略: 紧止损(市场好时容忍度低)
    if strategy=='momentum': stop=-atr_pct*1.2
    # reversion策略: 宽止损(等反弹需要时间)
    elif strategy=='reversion': stop=-atr_pct*2.5

    # 仓位越高止损越紧
    if position_pct>60: stop=max(stop,-0.03)
    elif position_pct>40: stop=max(stop,-0.05)
    else: stop=max(stop,-0.10)

    return round(stop,4)

# ═══ L3: 情绪辅助 ═══
# score_sentiment 已迁移至 engine/sentiment_scorer.py v2.0（资金流向+技术指标增强）
from engine.sentiment_scorer import score_sentiment, SentimentResult

# ═══ V型 ═══
def vmap_score(raw, center=25):
    dist=abs(raw-center)
    return round(min(100,max(0,dist*(100/(100-center)))),1)

# ═══ v4.0 主引擎 ═══
class ScoreEngineV4:
    def __init__(self, db=None):
        self.db=db or None
        self.conn=None
        self._industry_cache={}

    def _connect(self):
        if self.conn is None or not self.conn.open:
            self.conn=get_connection()

    def close(self):
        if self.conn and self.conn.open: self.conn.close()

    def _load_kline(self, ts_code, lookback=400):
        self._connect()
        cur=self.conn.cursor(pymysql.cursors.DictCursor)
        # 优先用复权K线，不足120日或最新日期不够新则回退到原始K线
        cur.execute("SELECT trade_date,high,low,close,vol,change_pct FROM daily_kline_qfq WHERE ts_code=%s ORDER BY trade_date ASC",(ts_code,))
        rows=cur.fetchall()
        # 动态获取最新交易日日期
        _latest_td = str(date.today())
        try:
            _c2 = self.conn.cursor()
            _c2.execute("SELECT MAX(trade_date) as d FROM daily_kline")
            _r2 = _c2.fetchone()
            if _r2 and _r2[0]:
                _latest_td = str(_r2[0])
            _c2.close()
        except: pass
        if len(rows) < 120 or (rows and str(rows[-1]['trade_date']) < _latest_td):
            cur.execute("SELECT trade_date,high,low,close,vol,change_pct FROM daily_kline WHERE ts_code=%s ORDER BY trade_date ASC",(ts_code,))
            rows2=cur.fetchall()
            if len(rows2) >= 120:
                rows = rows2
        cur.close()
        return rows[-lookback:] if len(rows)>lookback else rows

    def _get_industry(self, ts_code):
        if ts_code in self._industry_cache: return self._industry_cache[ts_code]
        self._connect()
        cur=self.conn.cursor(pymysql.cursors.DictCursor)
        cur.execute("SELECT industry FROM backtest_pool WHERE ts_code=%s",(ts_code,))
        r=cur.fetchone()
        ind=r['industry'] if r else None
        if not ind:
            cur.execute("SELECT industry FROM stock_basic WHERE ts_code=%s",(ts_code,))
            r2=cur.fetchone()
            ind=r2['industry'] if r2 else None
        cur.close()
        self._industry_cache[ts_code]=ind
        return ind

    def get_market_context(self):
        self._connect()
        cur=self.conn.cursor(pymysql.cursors.DictCursor)
        cur.execute("SELECT season, raw_score, confidence FROM season_state WHERE index_code='MARKET' ORDER BY trade_date DESC LIMIT 1")
        mr=cur.fetchone()
        season=mr['season'] if mr else 'chaos'
        mkt_score=float(mr['raw_score'] or 0) if mr else 0
        conf=float(mr.get('confidence') or 0) if mr and mr.get('confidence') else 0.6
        if conf<0.1: conf=0.6

        cur.execute("SELECT season, raw_score FROM season_state WHERE index_code='000300.SH' ORDER BY trade_date DESC LIMIT 1")
        idx300=cur.fetchone()
        regime='bull' if idx300 and float(idx300['raw_score'] or 0)>3 else ('bear' if idx300 and float(idx300['raw_score'] or 0)<-2 else 'range')

        cur.execute("SELECT MAX(trade_date) as d FROM daily_kline")
        ld=cur.fetchone()['d']
        cur.execute("SELECT COUNT(*) as total, SUM(CASE WHEN change_pct>0 THEN 1 ELSE 0 END) as up FROM daily_kline WHERE trade_date=%s",(ld,))
        br=cur.fetchone()
        breadth=br['up']/br['total'] if br and br['total'] else 0.5
        cur.close()
        return {'season':season,'regime':regime,'market_score':mkt_score,'confidence':conf,'breadth_ratio':breadth}

    def score_one(self, ts_code, mkt=None):
        if mkt is None: mkt=self.get_market_context()
        rows=self._load_kline(ts_code)
        if len(rows)<120: return {'ts_code':ts_code,'error':f'数据不足({len(rows)}日)'}

        closes=[float(r['close']) for r in rows]
        highs=[float(r['high']) for r in rows]
        lows=[float(r['low']) for r in rows]
        vols=[float(r.get('vol',0) or 0) for r in rows]
        chgs=[float(r.get('change_pct') or 0) for r in rows]
        industry=self._get_industry(ts_code)

        # 优化1: L1 增强
        cycle=score_cycle_enhanced(mkt['season'],mkt['regime'],mkt['market_score'],industry,closes)

        # 优化2+3: L2 缠论增强 + 板块权重
        chanlun=score_chanlun_enhanced(rows, cycle['strategy'], industry, ts_code=ts_code)
        bw=get_block_weights(industry)
        from engine.block_weights import adjust_weights_by_season
        bw=adjust_weights_by_season(bw, mkt['season'])
        # 用板块权重重新合成L2总分
        l2_raw=chanlun['trend']*bw['trend']+chanlun['momentum']*bw['momentum']+chanlun['volatility']*bw['volatility']+chanlun['volume']*bw['volume']
        chanlun_total=round(max(0,min(100,l2_raw+chanlun['chanlun_signal']*0.15)),1)

        # L3: 情绪 (v2.0: 资金流向+技术指标增强)
        r14=rsi(closes,14)
        v5m=sma(vols[-10:],5) if len(vols)>=10 else vols[-1]
        v20m=sma(vols[-25:],20) if len(vols)>=25 else v5m
        vol_reg='high' if v5m>v20m*1.3 else ('low' if v5m<v20m*0.7 else 'normal')
        # 从数据库读取资金流向和技术指标
        mf_net=None; mf_lg=None; mf_elg=None
        tc_macd=None; tc_boll=None; tc_kdj=None
        try:
            _cur=get_connection().cursor(pymysql.cursors.DictCursor)
            _cur.execute("SELECT net_mf_amount, buy_lg_amount-sell_lg_amount as lg_net, buy_elg_amount-sell_elg_amount as elg_net FROM moneyflow WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1",(ts_code,))
            _r=_cur.fetchone()
            if _r:
                mf_net=float(_r['net_mf_amount']or 0)
                mf_lg=float(_r['lg_net']or 0)
                mf_elg=float(_r['elg_net']or 0)
            _cur.execute("SELECT macd_bar, boll_upper, boll_mid, boll_lower, kdj_j FROM technical_indicator WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1",(ts_code,))
            _r2=_cur.fetchone()
            if _r2 and _r2['boll_upper'] and _r2['boll_mid'] and _r2['boll_lower']:
                tc_macd=float(_r2.get('macd_bar',0)or 0)
                b_up=float(_r2['boll_upper']); b_mid=float(_r2['boll_mid']); b_low=float(_r2['boll_lower'])
                if b_up> b_low and closes[-1]>0:
                    tc_boll=(closes[-1]-b_mid)/((b_up-b_low)/2) if (b_up-b_low)>0 else 0
                tc_kdj=float(_r2.get('kdj_j',50)or 50)
            _cur.close()
        except: pass
        sentiment=score_sentiment(mkt['breadth_ratio'],vol_reg,r14,chgs[-1] if chgs else 0,
            money_flow_net=mf_net, money_flow_lg=mf_lg, money_flow_elg=mf_elg,
            tech_macd_bar=tc_macd, tech_boll_pos=tc_boll, tech_kdj_j=tc_kdj)

        # ═══ P0-1: ATR+量能潮汐因子 ═══
        tide_boost = 0
        tide_detail = ''
        season = mkt.get('season', 'chaos')
        if len(closes) >= 60 and len(vols) >= 20:
            # 因子6: 波动率变化方向 (vol_change)
            atr_short = atr(highs, lows, closes, 14)
            atr_long = atr(highs, lows, closes, 60)
            atr_ratio = atr_short / max(atr_long, 0.001)
            # 波动收缩 → 酝酿方向确认
            if atr_ratio < 0.8:
                tide_boost += 4
                tide_detail += f'波动收缩(ATR比={atr_ratio:.2f}) +4; '
            elif atr_ratio > 1.3:
                # 波动放大 → 风险信号（混沌期权重加倍）
                chg_sign = 1 if chgs[-1] > 0 else -1
                boost = -4 * (2 if season in ('chaos','chaos_autumn') else 1)
                tide_boost += boost
                tide_detail += f'波动放大(ATR比={atr_ratio:.2f}) {boost:+d}; '
            else:
                tide_detail += f'波动正常(ATR比={atr_ratio:.2f}) +0; '

            # 因子7: 量能潮汐 (volume_tide)
            vol_ma20 = sma(vols, 20)
            vol_dev = (vols[-1] - vol_ma20) / max(vol_ma20, 1) * 100
            # 仅混沌期启用量能潮汐
            if season in ('chaos', 'chaos_spring', 'chaos_autumn'):
                if vol_dev > 30 and chgs[-1] > 0:
                    tide_boost += 5
                    tide_detail += f'放量上涨(量偏离{vol_dev:.0f}%) +5'
                elif vol_dev > 30 and chgs[-1] < 0:
                    tide_boost -= 3
                    tide_detail += f'放量下跌(量偏离{vol_dev:.0f}%) -3'
                elif vol_dev < -20:
                    tide_detail += f'缩量观望(量偏离{vol_dev:.0f}%) +0'
                else:
                    tide_detail += f'量能正常(量偏离{vol_dev:.0f}%) +0'
            else:
                # 非混沌期：缩量回踩是机会
                if vol_dev < -20 and chgs[-1] > 0:
                    tide_boost += 3
                    tide_detail += f'缩量上涨(健康,量偏离{vol_dev:.0f}%) +3'
                elif vol_dev > 30 and chgs[-1] > 0:
                    tide_boost += 2
                    tide_detail += f'放量上攻(量偏离{vol_dev:.0f}%) +2'
                else:
                    tide_detail += f'量能正常(量偏离{vol_dev:.0f}%) +0'

        # 三层合成（+ 潮汐因子校准）
        raw_score=cycle['score']*0.30+chanlun_total*0.40+sentiment.score*0.30 + tide_boost
        v_score=vmap_score(raw_score,25)

        # 仓位
        base_pos=50
        ch_factor=1.0
        if chanlun['trend']>=90: ch_factor=1.4
        elif chanlun['trend']>=70: ch_factor=1.2
        elif chanlun['trend']<30: ch_factor=0.5
        adj_pos=base_pos*ch_factor*mkt['confidence']
        adj_pos=max(5,min(100,adj_pos))

        # 优化4: ATR止损
        stop_loss=calc_stop_loss(closes,highs,lows,adj_pos,cycle['strategy'])

        # 优化5: 信号判定 (reversion严格收窄)
        strategy=cycle['strategy']
        if strategy=='momentum':
            if v_score>=50 and chanlun['trend']>=90: signal,sig_label='STRONG_BUY','🟢强烈买入'
            elif v_score>=45 and chanlun['trend']>=85: signal,sig_label='BUY','🟢买入'
            elif v_score>=40 and chanlun['trend']>=80: signal,sig_label='CAUTIOUS_BUY','🟡谨慎买入'
            elif chanlun['trend']<30 or v_score<15: signal,sig_label='SELL','🔴卖出'
            else: signal,sig_label='HOLD','⏸️持有'
        else:  # reversion
            if v_score>=50 and chanlun['volatility']<=35:
                signal,sig_label='REV_BUY','🔄超跌反转'
            elif chanlun['trend']>=80: signal,sig_label='HOLD','⏸️反弹中持有'
            elif v_score<15: signal,sig_label='SELL','🔴止损'
            else: signal,sig_label='WAIT','⏳等待信号'

        # 风险标记
        risk_flags=[]
        if chanlun['volatility']<=25: risk_flags.append('低波稳健')
        elif chanlun['volatility']>=45: risk_flags.append('⚠️高波动')
        if r14>75: risk_flags.append(f'RSI超买({r14:.0f})')
        if r14<25: risk_flags.append(f'RSI超卖({r14:.0f})')
        if mkt['breadth_ratio']<0.30: risk_flags.append('市场恐慌')
        if chanlun['chanlun_signal']<-30: risk_flags.append('缠论顶背离')
        elif chanlun['chanlun_signal']>30: risk_flags.append('缠论底背离')

        # 周期收益
        rets={}
        for p in [5,10,20,30]:
            if len(closes)>p:
                rets[p]=round((closes[-1]-closes[-p-1])/closes[-p-1]*100,2)

        return {
            'ts_code':ts_code,'trade_date':rows[-1]['trade_date'],
            'close':round(closes[-1],2),'change_pct':round(chgs[-1] if chgs else 0,2),
            'industry':industry or '未知',
            # 三层
            'cycle_score':cycle['score'],'chanlun_score':chanlun_total,'sentiment_score':sentiment.score,
            'raw_score':round(raw_score,1),'v_score':v_score,
            # 拆解
            'trend_score':chanlun['trend'],'momentum_score':chanlun['momentum'],
            'volatility_score':chanlun['volatility'],'volume_score':chanlun['volume'],
            'chanlun_signal':chanlun['chanlun_signal'],
            'tide_boost':tide_boost,'tide_detail':tide_detail,
            'sector_boost':cycle.get('sector_boost',0),
            # 板块权重
            'block_weights':{k:round(v,2) for k,v in bw.items()},
            # 信号
            'strategy':strategy,'signal':signal,'signal_label':sig_label,
            'position_pct':round(adj_pos,1),'stop_loss_pct':stop_loss,
            'confidence':round(mkt['confidence'],3),'risk_flags':risk_flags,
            # 市场
            'season':mkt['season'],'regime':mkt['regime'],'breadth':round(mkt['breadth_ratio']*100,1),
            **{f'ret_{p}d':rets.get(p,0) for p in [5,10,20,30]},
        }

    def score_pool(self, limit=None, save_db=False):
        mkt=self.get_market_context()
        self._connect()
        cur=self.conn.cursor(pymysql.cursors.DictCursor)
        cur.execute("SELECT ts_code, name, industry FROM backtest_pool WHERE status='ACTIVE' AND market!='指数' ORDER BY ts_code")
        stocks=cur.fetchall(); cur.close()
        results=[]
        saved_ts=0
        saved_ss=0
        for s in stocks:
            r=self.score_one(s['ts_code'],mkt)
            if 'error' not in r:
                r['name']=s.get('name','')
                r['industry']=s.get('industry', r.get('industry',''))
                results.append(r)
                if save_db:
                    try:
                        trade_date = r['trade_date']
                        if isinstance(trade_date, str): trade_date = trade_date[:10]
                        cur2=self.conn.cursor()
                        # 1) 写 trend_score
                        cur2.execute("""
                            INSERT INTO trend_score
                                (ts_code, trade_date, cycle_score, structure_score, emotion_score,
                                 composite_score, confidence_mult, raw_score, is_calculable, close_price)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,%s)
                            ON DUPLICATE KEY UPDATE
                                cycle_score=VALUES(cycle_score),
                                structure_score=VALUES(structure_score),
                                emotion_score=VALUES(emotion_score),
                                composite_score=VALUES(composite_score),
                                confidence_mult=VALUES(confidence_mult),
                                raw_score=VALUES(raw_score),
                                close_price=VALUES(close_price)
                        """, (
                            r['ts_code'], trade_date,
                            round(r['cycle_score'],2),
                            round(r['chanlun_score'],2),
                            round(r['sentiment_score'],2),
                            round(r['v_score'],2),
                            round(r.get('confidence',1.0),2),
                            round(r['raw_score'],2),
                            round(r.get('close',0),3),
                        ))
                        saved_ts+=1

                        # 2) 写 strategy_signal (驾驶舱用)
                        direction = 'LONG'
                        if r['signal'] in ('SELL','STRONG_SELL'):
                            direction = 'SHORT'
                        elif r['signal'] in ('WAIT','HOLD'):
                            direction = 'NEUTRAL'
                        
                        # operation_mode
                        strategy = r.get('strategy','momentum')
                        if strategy=='momentum':
                            op_mode = 'attack' if r['v_score']>=35 else 'normal'
                        else:
                            op_mode = 'defense' if r['v_score']>=35 else 'dormant'

                        # 仓位对应
                        pos_pct = round(r.get('position_pct', 50), 2)

                        # 止损价
                        close_price = r.get('close', 0)
                        stop_pct = r.get('stop_loss_pct', -0.05)
                        stop_loss_price = round(close_price * (1 + stop_pct), 3) if close_price else 0

                        # 理由链
                        reason = f"{mkt['season']}+{r.get('signal_label','?')}"
                        risk_str = '|'.join(r.get('risk_flags',[])) if r.get('risk_flags') else ''
                        if risk_str:
                            reason += f" [{risk_str}]"

                        # 信号置信度
                        if r['v_score']>=45:
                            sig_conf = 'high'
                        elif r['v_score']>=30:
                            sig_conf = 'medium'
                        else:
                            sig_conf = 'low'

                        # 从chanlun_structure读取秋老虎数据
                        autumn_tiger_flag = 0
                        tiger_conf_val = 0.0
                        tiger_reasons_str = None
                        try:
                            _ct_cur = get_connection().cursor(pymysql.cursors.DictCursor)
                            _ct_cur.execute(
                                "SELECT autumn_tiger, tiger_confidence, tiger_reasons FROM chanlun_structure WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1",
                                (r['ts_code'],)
                            )
                            _ct_row = _ct_cur.fetchone()
                            if _ct_row:
                                autumn_tiger_flag = int(_ct_row['autumn_tiger'] or 0)
                                tiger_conf_val = float(_ct_row['tiger_confidence'] or 0)
                                tiger_reasons_str = str(_ct_row['tiger_reasons'] or '')[:200]
                            _ct_cur.close()
                        except:
                            pass

                        # ═══ 安全闸门判定 ═══
                        gate_triggered = 0
                        safety_gate_reason = None
                        confidence = float(r.get('confidence', mkt.get('confidence', 0.5)))
                        season = mkt.get('season', 'chaos')
                        breadth = mkt.get('breadth_ratio', 0.5)
                        # ═══ 市场状态参数查表 ═══
                        # 根据季节/市场状态获取动态参数
                        # 从 system_config 表动态读取季节参数
                        _sc = {}
                        try:
                            _cur3 = self.conn.cursor()
                            _cur3.execute("SELECT config_key, config_value FROM system_config")
                            for _r3 in _cur3.fetchall():
                                _sc[_r3[0]] = _r3[1]
                            _cur3.close()
                        except: pass
                        _season_map = {
                            'summer':       ('summer',       'momentum'),
                            'spring':       ('spring',       'momentum'),
                            'chaos_spring': ('chaos_spring', 'momentum'),
                            'chaos':        ('chaos',        '空仓'),
                            'chaos_autumn': ('chaos_autumn', 'reversion'),
                            'autumn':       ('autumn',       'reversion'),
                            'winter':       ('winter',       'reversion'),
                            'panic':        ('panic',        '空仓'),
                        }
                        _sp = _season_map.get(season, _season_map['chaos'])
                        _prefix = 'season_' + _sp[0] + '_'
                        _buy = _sc.get(_prefix + 'buy_threshold', 40)
                        _maxpos = _sc.get(_prefix + 'max_pos', 10)
                        _stop = _sc.get(_prefix + 'stop_loss', -5)
                        _hold = _sc.get(_prefix + 'min_hold', 0)
                        sp = {
                            'buy_th': int(_buy),
                            'max_pos': int(_maxpos),
                            'stop': float(_stop),
                            'min_hold': int(_hold),
                            'strategy': _sp[1],
                        }

                        # 安全闸门判定
                        gate_triggered = 0
                        safety_gate_reason = None
                        confidence = float(r.get('confidence', mkt.get('confidence', 0.5)))
                        breadth = mkt.get('breadth_ratio', 0.5)

                        # 规则1: 纯混沌→强制空仓
                        if season == 'chaos':
                            gate_triggered = 1
                            safety_gate_reason = '🌪️混沌震荡, 自动空仓'
                        # 规则2: 恐慌→强制空仓
                        elif season in ('panic',):
                            gate_triggered = 1
                            safety_gate_reason = '💀市场恐慌, 强制空仓'
                        # 规则3: 低置信度
                        if not gate_triggered and confidence < 0.3:
                            gate_triggered = 1
                            safety_gate_reason = '置信度过低({:.0f}%)'.format(confidence*100)
                        # 规则4: 极端宽度
                        if not gate_triggered and breadth < 0.20:
                            gate_triggered = 1
                            safety_gate_reason = '市场宽度极端({:.0f}%)'.format(breadth*100)
                        elif not gate_triggered and breadth > 0.85:
                            gate_triggered = 1
                            safety_gate_reason = '市场过热宽度({:.0f}%)'.format(breadth*100)

                        # 动态调整仓位、方向、止损
                        if gate_triggered:
                            final_direction = 'NEUTRAL'
                            final_pos_pct = min(pos_pct, sp['max_pos'])
                            final_sig_conf = 'low'
                        else:
                            final_direction = direction
                            final_pos_pct = min(pos_pct, sp['max_pos'])
                            final_sig_conf = sig_conf

                        cur2.execute("""
                            INSERT INTO strategy_signal
                                (ts_code, trade_date, cycle_stage, composite_score, direction,
                                 position_pct, stop_loss, reason_chain, operation_mode,
                                 signal_confidence, is_calculable, entry_low, entry_high,
                                 autumn_tiger, tiger_confidence, gate_triggered, safety_gate)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s)
                            ON DUPLICATE KEY UPDATE
                                cycle_stage=VALUES(cycle_stage),
                                composite_score=VALUES(composite_score),
                                direction=VALUES(direction),
                                position_pct=VALUES(position_pct),
                                stop_loss=VALUES(stop_loss),
                                reason_chain=VALUES(reason_chain),
                                operation_mode=VALUES(operation_mode),
                                signal_confidence=VALUES(signal_confidence),
                                entry_low=VALUES(entry_low),
                                entry_high=VALUES(entry_high),
                                autumn_tiger=VALUES(autumn_tiger),
                                tiger_confidence=VALUES(tiger_confidence),
                                gate_triggered=VALUES(gate_triggered),
                                safety_gate=VALUES(safety_gate)
                        """, (
                            r['ts_code'], trade_date,
                            mkt.get('regime','range'),
                            round(r['v_score'],2),
                            final_direction, final_pos_pct, stop_loss_price,
                            reason[:200], op_mode, final_sig_conf,
                            round(close_price*0.98, 3) if close_price else 0,
                            close_price,
                            autumn_tiger_flag, round(tiger_conf_val, 2),
                            gate_triggered, safety_gate_reason,
                        ))
                        saved_ss+=1
                        cur2.close()
                    except Exception as e:
                        print(f"  ⚠️ 入库失败 {r['ts_code']}: {e}")
            if saved_ss>0 and saved_ss%50==0:
                self.conn.commit()
        if saved_ss>0:
            self.conn.commit()
            print(f"  💾 已入库 {saved_ts} 条到 trend_score + {saved_ss} 条到 strategy_signal (trade_date={results[0]['trade_date']})")
        results.sort(key=lambda x: x['v_score'], reverse=True)
        return results[:limit] if limit else results

# ═══ CLI ═══
def main():
    import json,argparse
    ap=argparse.ArgumentParser(description='评分引擎 v4.0 五优化强化版')
    ap.add_argument('--top',type=int,default=100)
    ap.add_argument('--stock',type=str)
    ap.add_argument('--json',action='store_true')
    ap.add_argument('--no-save',action='store_true',help='不写库，仅打印')
    args=ap.parse_args()
    e=ScoreEngineV4()
    try:
        if args.stock:
            r=e.score_one(args.stock)
            if args.json: print(json.dumps(r,indent=2,ensure_ascii=False,default=str))
            else:
                print(f"\n{r['ts_code']} {r.get('name','')} close={r['close']:.2f} industry={r.get('industry','?')}")
                print(f"三层: L1={r['cycle_score']:.0f}+L2={r['chanlun_score']:.0f}+L3={r['sentiment_score']:.0f}={r['raw_score']:.1f}")
                print(f"V分={r['v_score']:.1f} | {r['signal_label']} | 仓位={r['position_pct']:.0f}% | 止损={r['stop_loss_pct']*100:+.1f}%")
                print(f"板块权重: {r.get('block_weights',{})}")
                print(f"缠论信号: {r['chanlun_signal']:+.0f} | 板块增强: {r.get('sector_boost',0):+.0f}")
                if r['risk_flags']: print(f"⚠️ {'|'.join(r['risk_flags'])}")
        else:
            save = not args.no_save
            results=e.score_pool(args.top, save_db=save)
            mkt=e.get_market_context()
            print(f"\n📊 v4.0 | {mkt['season']} {mkt['regime']} breadth={mkt['breadth_ratio']*100:.0f}%")
            print(f"  {'代码':>12s} {'名称':>8s} {'行业':>8s} {'L1':>4s} {'L2':>4s} {'L3':>4s} {'V分':>5s} {'信号':>14s} {'仓位':>5s} {'止损%':>6s}")
            for r in results:
                print(f"  {r['ts_code']:>12s} {r.get('name','')[:6]:>8s} {r.get('industry','?')[:6]:>8s} "
                      f"{r['cycle_score']:>4.0f} {r['chanlun_score']:>4.0f} {r['sentiment_score']:>4.0f} "
                      f"{r['v_score']:>5.1f} {r['signal_label']:>14s} {r['position_pct']:>4.0f}% {r['stop_loss_pct']*100:>+5.1f}%")
    finally:
        e.close()

if __name__=='__main__': main()
