#!/usr/bin/env python3
"""
tide_ic_validate.py - Tide评分IC验证 + 对比工具

功能:
  1. 对多期历史数据跑Tide评分
  2. 计算每个因子的 IC（信息系数，RankIC + NormalIC）
  3. Tide vs V4 评分对比（方差、均值、分布）
  4. 回测收益验证

输出:
  - 打印IC报告到控制台
  - 写入 tide_ic_result 临时分析表
"""
import os, sys, json, math, logging
from datetime import date, timedelta, datetime
from typing import Dict, List, Tuple, Optional

_backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
if _backend_dir not in sys.path: sys.path.insert(0, _backend_dir)
from db_config import get_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('tide_ic')

# ==================== 因子计算函数 ====================

def _compute_single_factor(ts_code: str, trade_date: str, factor_name: str) -> float:
    """计算单个因子值（从DB已有tide_factor_value读取）"""
    col_map = {'f1': 'f1_score', 'f3': 'f3_score', 'f4': 'f4_score', 
               'f5': 'f5_score', 'f6': 'f6_score'}
    col = col_map.get(factor_name)
    if not col:
        return 0.0
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT {col} FROM tide_factor_value
        WHERE ts_code=%s AND trade_date=%s
    """, (ts_code, trade_date))
    row = cur.fetchone()
    cur.close(); conn.close()
    if row and row[col] is not None:
        return float(row[col])
    return 0.0


def _get_future_return(ts_code: str, trade_date: str, days: int = 5) -> Optional[float]:
    """获取未来N日收益率"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT close FROM daily_kline
        WHERE ts_code=%s AND trade_date > %s AND is_valid=1
        ORDER BY trade_date ASC LIMIT %s
    """, (ts_code, trade_date, days))
    rows = [float(r['close']) for r in cur.fetchall()]
    cur.close(); conn.close()
    if len(rows) < days:
        return None
    entry_close = None
    # 获取当日的收盘价
    cur2 = conn = get_connection()
    cur2 = cur2.cursor()
    cur2.execute("""
        SELECT close FROM daily_kline
        WHERE ts_code=%s AND trade_date=%s AND is_valid=1
    """, (ts_code, trade_date))
    row = cur2.fetchone()
    cur2.close()
    if row:
        entry_close = float(row['close'])
    cur2.close()
    if entry_close is None or entry_close == 0:
        return None
    future_close = rows[days - 1]
    return (future_close - entry_close) / entry_close


# ==================== IC 计算 ====================

def _rank_ic(factor_values: List[float], forward_returns: List[float]) -> Optional[float]:
    """RankIC: Spearman秩相关系数"""
    n = len(factor_values)
    if n < 10:
        return None
    # 去掉None
    pairs = [(fv, fr) for fv, fr in zip(factor_values, forward_returns) 
             if fv is not None and fr is not None]
    if len(pairs) < 10:
        return None
    f_vals = [p[0] for p in pairs]
    r_vals = [p[1] for p in pairs]
    # 排名
    def _rank(vals):
        sorted_v = sorted(set(vals))
        rank_map = {v: i + 1 for i, v in enumerate(sorted_v)}
        return [rank_map[v] for v in vals]
    rank_f = _rank(f_vals)
    rank_r = _rank(r_vals)
    n = len(rank_f)
    d_sum = sum((rf - rr) ** 2 for rf, rr in zip(rank_f, rank_r))
    return 1.0 - (6.0 * d_sum) / (n * (n * n - 1))


def _normal_ic(factor_values: List[float], forward_returns: List[float]) -> Optional[float]:
    """NormalIC: Pearson相关系数"""
    pairs = [(fv, fr) for fv, fr in zip(factor_values, forward_returns)
             if fv is not None and fr is not None]
    if len(pairs) < 10:
        return None
    f_vals = [p[0] for p in pairs]
    r_vals = [p[1] for p in pairs]
    n = len(f_vals)
    mean_f = sum(f_vals) / n
    mean_r = sum(r_vals) / n
    cov = sum((f_vals[i] - mean_f) * (r_vals[i] - mean_r) for i in range(n))
    var_f = sum((x - mean_f) ** 2 for x in f_vals)
    var_r = sum((x - mean_r) ** 2 for x in r_vals)
    if var_f == 0 or var_r == 0:
        return None
    return cov / (math.sqrt(var_f) * math.sqrt(var_r))


# ==================== Tide评分运行 ====================

def _get_ts_code_list() -> List[str]:
    """获取股票列表（使用监控池）"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
    codes = [r['ts_code'] for r in cur.fetchall()]
    # 如果不够多，扩到 stock_pool
    if len(codes) < 100:
        cur.execute("SELECT DISTINCT ts_code FROM daily_kline WHERE trade_date >= '2026-06-01' LIMIT 500")
        codes = list(set([r['ts_code'] for r in cur.fetchall()]))
    cur.close(); conn.close()
    return codes


def _get_tide_score(ts_code: str, trade_date: str, factors: Dict[str, float] = None) -> Tuple[float, float, str]:
    """计算Tide评分（复用scorer中逻辑）"""
    from tide_engine.tide_config import get_factor_weights
    from tide_engine.tide_chanlun_layer import apply_chanlun_layer

    season = _get_season(trade_date)
    weights = get_factor_weights()

    if factors is None:
        factors = {}
        for fn in ['f1', 'f3', 'f4', 'f5', 'f6']:
            try:
                factors[fn] = _compute_single_factor(ts_code, trade_date, fn)
            except:
                factors[fn] = 0.0

    # L3 分
    l3 = sum(factors.get(f'f{i}', 0.0) * weights.get(f'f{i}', 0.0) for i in [1, 3, 4, 5, 6])
    l3_mapped = max(0, min(100, (l3 + 5) / 10 * 100))

    # 缠论
    try:
        cl_result = apply_chanlun_layer(ts_code, trade_date, factors, season)
        bonus = cl_result['bonus']
    except:
        bonus = 0.0

    tide_score = max(0, min(100, l3_mapped + bonus))
    track = 'momentum' if tide_score >= 50 else 'reversion'
    label = '买入' if tide_score >= 60 else ('关注' if tide_score >= 40 else '观望')
    return round(tide_score, 2), round(l3_mapped, 2), track, label, season


def _get_season(trade_date: str) -> str:
    """获取季节"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT market_season FROM season_state 
            WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 1
        """, (trade_date,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row: return row['market_season']
    except:
        pass
    return 'summer'


def _save_ic_result(trade_date: str, factor_name: str, rank_ic: float, normal_ic: float, count: int):
    """保存IC结果到tide_config"""
    if rank_ic is None: rank_ic = 0.0
    if normal_ic is None: normal_ic = 0.0
    config_key = f'ic_{factor_name}'
    config_val = json.dumps({
        'trade_date': trade_date,
        'rank_ic': round(rank_ic, 4),
        'normal_ic': round(normal_ic, 4),
        'count': count,
        'season': _get_season(trade_date)
    })
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO tide_config (config_key, config_value, description)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE config_value=VALUES(config_value)
        """, (config_key, config_val, f'{factor_name} IC验证结果'))
        conn.commit()
        cur.close(); conn.close()
    except:
        pass


def _load_previous_ic(factor_name: str) -> List[Dict]:
    """加载历史IC记录"""
    results = []
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT config_value FROM tide_config 
            WHERE config_key LIKE %s
        """, (f'ic_{factor_name}_%',))
        for r in cur.fetchall():
            try:
                results.append(json.loads(r['config_value']))
            except:
                pass
        cur.close(); conn.close()
    except:
        pass
    return results


# ==================== V4对比 ====================

def _get_v4_scores(trade_date: str):
    """获取V4引擎同一日期评分(从backtest_score_v4表)"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT ts_code, total_score FROM backtest_score_v4 
        WHERE trade_date <= %s AND total_score IS NOT NULL
        ORDER BY trade_date DESC LIMIT 1
    """, (trade_date,))
    v4_date_row = cur.fetchone()
    if not v4_date_row or 'trade_date' not in v4_date_row:
        cur.close(); conn.close()
        return {}, None
    v4_date = v4_date_row['trade_date']
    cur.execute("""
        SELECT ts_code, total_score FROM backtest_score_v4 
        WHERE trade_date = %s AND total_score IS NOT NULL
    """, (v4_date,))
    result = {r['ts_code']: float(r['total_score']) for r in cur.fetchall()}
    cur.close(); conn.close()
    return result, str(v4_date)


# ==================== 主验证流程 ====================

def _get_historical_trade_dates(count: int = 30) -> List[str]:
    """获取既有Tide因子数据又有未来收益K线的历史交易日（用于IC验证）"""
    conn = get_connection()
    cur = conn.cursor()
    # 找有tide因子 && 20日之后还有K线的日期
    cur.execute("""
        SELECT DISTINCT tv.trade_date
        FROM tide_factor_value tv
        WHERE EXISTS (
            SELECT 1 FROM daily_kline k2 
            WHERE k2.trade_date > DATE_ADD(tv.trade_date, INTERVAL 10 DAY)
        )
        ORDER BY tv.trade_date DESC LIMIT %s
    """, (count,))
    dates = [str(r['trade_date']) for r in cur.fetchall()]
    cur.close(); conn.close()
    return dates


def run_ic_validation(trade_dates: List[str] = None) -> Dict:
    """
    IC验证主流程
    
    对多个交易日运行:
      1. 对每只股票计算5个因子
      2. 对每个因子计算IC(与未来5日收益)
      3. 对比Tide vs V4评分
    """
    if trade_dates is None or len(trade_dates) == 0:
        trade_dates = _get_historical_trade_dates(20)
    
    logger.info(f'[IC验证] 交易日期数: {len(trade_dates)}')
    
    all_results = {}
    factor_names = ['f1', 'f3', 'f4', 'f5', 'f6']
    has_multi_date = len(trade_dates) >= 2
    
    for factor_name in factor_names:
        logger.info(f'[IC验证] 计算因子 {factor_name}...')
        ic_list = []
        
        for td in trade_dates[:10]:  # 最多10个交易日
            # 批量从DB读取因子值
            conn = get_connection()
            cur = conn.cursor()
            col_map = {'f1': 'f1_score', 'f3': 'f3_score', 'f4': 'f4_score',
                       'f5': 'f5_score', 'f6': 'f6_score'}
            col = col_map.get(factor_name, 'f1_score')
            cur.execute(f"""
                SELECT tv.ts_code, tv.{col}
                FROM tide_factor_value tv
                WHERE tv.trade_date=%s
                LIMIT 300
            """, (td,))
            rows = cur.fetchall()
            cur.close(); conn.close()
            
            f_values = []
            r_values = []
            
            for r in rows:
                code = r['ts_code']
                fv = float(r[col]) if r[col] is not None else 0.0
                fr = _get_future_return(code, td, 5)
                if fv is not None and fr is not None:
                    f_values.append(fv)
                    r_values.append(fr)
            
            ric = _rank_ic(f_values, r_values)
            nic = _normal_ic(f_values, r_values)
            
            if ric is not None:
                ic_list.append(ric)
                logger.info(f'  {td}: RankIC={ric:.4f}, NormalIC={nic or 0:.4f}, n={len(f_values)}')
                _save_ic_result(td, factor_name, ric, nic, len(f_values))
        
        avg_ric = sum(ic_list) / len(ic_list) if ic_list else 0
        all_results[factor_name] = {
            'rank_ic_mean': round(avg_ric, 4),
            'rank_ic_std': round(math.sqrt(sum((x - avg_ric)**2 for x in ic_list) / len(ic_list)), 4) if len(ic_list) > 1 else 0,
            'sample_count': len(ic_list),
            'pass_tc1': abs(avg_ric) > 0.02
        }
        logger.info(f'  [{factor_name}] 平均RankIC={avg_ric:.4f} {"✅ PASS" if abs(avg_ric) > 0.02 else "❌ FAIL"}(阈值0.02)')
    
    # ---- Tide vs V4 评分对比 ----
    latest_td = trade_dates[0] if trade_dates else '2026-07-03'
    logger.info(f'[对比] 最新Tide日期: {latest_td}')
    
    tid_scores = {}  # ts_code -> tide_score
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT ts_code, tide_score, l3_score, chanlun_bonus, tide_track, tide_label
        FROM tide_score_signal WHERE trade_date=%s
    """, (latest_td,))
    for r in cur.fetchall():
        tid_scores[r['ts_code']] = {
            'tide_score': float(r['tide_score']),
            'l3_score': float(r['l3_score']) if r['l3_score'] else 0,
            'chanlun_bonus': float(r['chanlun_bonus']) if r['chanlun_bonus'] else 0,
            'track': r['tide_track'],
            'label': r['tide_label'],
        }
    cur.close(); conn.close()
    
    all_results['_tide_summary'] = {
        'trade_date': latest_td,
        'total_stocks': len(tid_scores),
        'tide_score_mean': round(sum(s['tide_score'] for s in tid_scores.values()) / len(tid_scores), 2) if tid_scores else 0,
        'tide_score_std': round(
            math.sqrt(sum((s['tide_score'] - sum(s2['tide_score'] for s2 in tid_scores.values()) / len(tid_scores))**2 
                      for s in tid_scores.values()) / len(tid_scores)), 2
        ) if tid_scores and len(tid_scores) > 1 else 0,
        'label_buy': sum(1 for s in tid_scores.values() if s['label'] == '买入'),
        'label_watch': sum(1 for s in tid_scores.values() if s['label'] == '关注'),
        'label_wait': sum(1 for s in tid_scores.values() if s['label'] == '观望'),
    }
    
    # V4 对比
    v4_data, v4_date = _get_v4_scores(latest_td)
    if v4_data:
        tide_keys = set(tid_scores.keys())
        common = tide_keys & set(v4_data.keys())
        if common:
            tide_vals = [tid_scores[c]['tide_score'] for c in common]
            v4_vals = [v4_data[c] for c in common]
            tide_mean = sum(tide_vals) / len(tide_vals)
            v4_mean = sum(v4_vals) / len(v4_vals)
            tide_var = sum((x - tide_mean)**2 for x in tide_vals) / len(tide_vals)
            v4_var = sum((x - v4_mean)**2 for x in v4_vals) / len(v4_vals)
            var_ratio = tide_var / v4_var if v4_var > 0 else 999
            
            all_results['_v4_compare'] = {
                'v4_trade_date': v4_date,
                'common_count': len(common),
                'tide_mean': round(tide_mean, 2),
                'v4_mean': round(v4_mean, 2),
                'tide_variance': round(tide_var, 2),
                'v4_variance': round(v4_var, 2),
                'tide_v4_variance_ratio': round(var_ratio, 2),
                'tide_v4_var_ratio_pass_tc2': var_ratio >= 1.2,
            }
    
    # 打印总结
    logger.info('=' * 50)
    logger.info('Tide IC验证报告')
    logger.info('=' * 50)
    for fn in factor_names:
        r = all_results[fn]
        logger.info(f'  {fn}: RankIC均值={r["rank_ic_mean"]:.4f} (std={r["rank_ic_std"]:.4f}) '
                    f'n={r["sample_count"]} {"✅" if r["pass_tc1"] else "❌"}')
    logger.info('')
    if '_tide_summary' in all_results:
        s = all_results['_tide_summary']
        logger.info(f'Tide评分: mean={s["tide_score_mean"]:.2f} std={s["tide_score_std"]:.2f}')
        logger.info(f'  买入={s["label_buy"]} 关注={s["label_watch"]} 观望={s["label_wait"]}')
    if '_v4_compare' in all_results:
        c = all_results['_v4_compare']
        logger.info(f'V4对比(截至{c["v4_trade_date"]}): {c["common_count"]}只重合')
        logger.info(f'  Tide方差={c["tide_variance"]:.2f} V4方差={c["v4_variance"]:.2f}')
        logger.info(f'  方差比={c["tide_v4_variance_ratio"]:.2f} {"✅ TC2" if c["tide_v4_var_ratio_pass_tc2"] else "❌ TC2"}')
    logger.info('=' * 50)
    
    return all_results


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Tide IC验证工具')
    parser.add_argument('--dates', nargs='+', help='指定交易日期(日期的后向收益用来计算的)')
    args = parser.parse_args()
    
    result = run_ic_validation(args.dates)
    # 保存完整结果
    result_path = os.path.join(os.path.dirname(__file__), '..', '..', 'tide_ic_report.json')
    with open(result_path, 'w') as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)
    logger.info(f'完整报告已保存到: {result_path}')
