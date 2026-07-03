#!/usr/bin/env python3
"""
阶梯动态持有策略引擎 v3.0 — 统一P6评分源
=========================================
2026-06-03 Tony要求彻底统一评分引擎

规则（v2.1 基于P6 Top N回测优化）：
  买入条件：综合评分≥75（仅高分段，May建议P0）
  5日检查点：评分≥40续持，否则平仓
  15日检查点：评分≥30续持，否则平仓
  25日检查点：评分≥20续持，否则平仓
  30日强制离场（长持有胜率下降，无需再续）
  全程止损：从最高点回撤≥10%时平仓
  移动止盈：从最高点回撤≥15%时止盈（May建议P1）
  最大持有30日

架构变更（2026-06-03）：
  评分 → 统一由 P6双轨引擎 (p6_dual_track_engine.daily_pipeline) 计算
  本引擎仅做：持仓检查点评估、买卖信号、冷却期控制
  评分源统一从 strategy_signal 表的 calibrated_score 字段读取

部署：由 run_strategy_daily.sh 每日16:00触发
"""

import pymysql, sys, json, os
from datetime import datetime, date, timedelta
from collections import defaultdict

# ─── P6引擎路径 ───
P6_PROJECT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, P6_PROJECT)

# ─── DB连接 ───
def get_pwd():
    try:
        with open('/etc/mysql/debian.cnf') as f:
            for l in f:
                if 'password' in l:
                    return l.split('=')[-1].strip().strip('"').strip("'")
    except: pass
    return ''

PWD = get_pwd()
DB = {'host':'127.0.0.1','port':3306,'user':'debian-sys-maint','password':PWD,'database':'stock_db_v2','charset':'utf8mb4'}

def _get_ts_token():
    """从MySQL读取Tushare Token"""
    conn = pymysql.connect(host='127.0.0.1', port=3306, user='debian-sys-maint', password=PWD, charset='utf8mb4', database='openclaw_config')
    cur = conn.cursor()
    cur.execute("SELECT api_key FROM api_credentials WHERE id=1")
    val = cur.fetchone()[0]
    cur.close(); conn.close()
    return val


def get_conn():
    return pymysql.connect(**DB)


# ─── 实时行情获取（Tushare rt_k API） ───
def fetch_realtime_price(ts_code, retries=2):
    """获取实时股价——使用Tushare rt_k接口（盘中实时），失败次数fallback"""
    import urllib.request, json, time
    
    token = _get_ts_token()
    url = "http://api.waditu.com/"
    payload = json.dumps({
        "api_name": "rt_k",
        "token": token,
        "params": {"ts_code": ts_code},
        "fields": ""
    })
    
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=payload.encode(), headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=15)
            j = json.loads(resp.read().decode())
            if j.get("data") and j["data"].get("items"):
                item = j["data"]["items"][0]
                cols = j["data"]["fields"]
                d = dict(zip(cols, item))
                now_price = float(d.get("close", 0) or 0)
                pre_close = float(d.get("pre_close", 0) or 0)
                if now_price > 0 and pre_close > 0:
                    return {
                        'price': now_price,
                        'prev_close': pre_close,
                        'change_pct': round((now_price - pre_close) / pre_close * 100, 2),
                        'realtime': True,
                        'source': 'tushare_rt_k',
                    }
        except Exception:
            if attempt < retries:
                time.sleep(3)
            continue
    return None


# ════════════════════════════════════════════
# 策略加载
# ════════════════════════════════════════════

def load_strategy_configs():
    """加载所有活跃策略"""
    conn = get_conn()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT * FROM strategy_config WHERE 1=1")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


# ════════════════════════════════════════════
# 核心：运行P6评分 → 策略评估
# ════════════════════════════════════════════

def run_p6_pipeline(trade_date=None):
    """
    调用P6双轨引擎做全量评分，写入 strategy_signal
    """
    from p6_dual_track_engine import daily_pipeline, MarketContext
    from season_engine import SeasonEngine
    
    print("  🏃 P6双轨评分管道...")
    results = daily_pipeline(mode='watch_pool')
    print(f"  ✅ P6评分完成: {len(results)}只")
    return results


def evaluate_strategy(trade_date, strategy):
    """
    策略评估——只做持仓检视和买卖信号，不自己算评分
    评分源：strategy_signal.calibrated_score（P6引擎写入）
    """
    sid = strategy['id']
    buy_min = strategy['buy_min_score']
    p1 = strategy['p1_score']
    p2 = strategy['p2_score']
    p3 = strategy['p3_score']
    sl_pct = float(strategy['stop_loss_pct'])
    max_hold = strategy['max_hold_days']
    cool_days = strategy['cool_days']
    
    sl_ratio = sl_pct / 100.0
    trade_date_str = trade_date.strftime('%Y-%m-%d') if isinstance(trade_date, date) else trade_date
    
    conn = get_conn()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # 1. 获取监控池股票
    cur.execute("SELECT ts_code, name FROM watch_pool WHERE 1=1")
    stocks = {r['ts_code']: r['name'] for r in cur.fetchall()}
    
    # 2. 获取P6评分（统一评分源）
    cur.execute("""
        SELECT ts_code, composite_score, calibrated_score, track, scoring_strategy
        FROM strategy_signal
        WHERE trade_date=%s
    """, (trade_date_str,))
    p6_map = {}
    for r in cur.fetchall():
        calibrated = float(r['calibrated_score'] or r['composite_score'] or 0)
        p6_map[r['ts_code']] = {
            'score': calibrated,
            'track': r.get('track', ''),
        }
    
    # 3. 获取持仓
    cur.execute("""
        SELECT ts_code, name, buy_date, cost_price, current_price, profit_pct, 
               shares, NULL as lock_until, 0 as lock_active
        FROM portfolio_holdings 
        WHERE status='HOLDING'
    """)
    holdings = {r['ts_code']: r for r in cur.fetchall()}
    
    # 4. 获取K线（仅用于持仓天数计算和价格展示）
    # 批加载最近250日K线
    cur.execute("""
        SELECT ts_code, trade_date, close, high, low
        FROM daily_kline_qfq
        WHERE trade_date >= %s AND trade_date <= %s
        ORDER BY ts_code, trade_date ASC
    """, (
        (datetime.strptime(trade_date_str, '%Y-%m-%d') - timedelta(days=365)).strftime('%Y-%m-%d'),
        trade_date_str,
    ))
    kline_map = defaultdict(list)
    for r in cur.fetchall():
        kline_map[r['ts_code']].append({
            'trade_date': str(r['trade_date']),
            'close': float(r['close']),
            'high': float(r['high']),
            'low': float(r['low']),
        })
    
    results = []
    
    for ts_code, name in stocks.items():
        current_score = p6_map.get(ts_code, {}).get('score', None)
        if current_score is None or current_score <= 0:
            continue  # P6无评分则跳过
        
        klines = kline_map.get(ts_code, [])
        if len(klines) < 200:
            continue
        
        dates = [k['trade_date'] for k in klines]
        closes = [k['close'] for k in klines]
        highs = [k['high'] for k in klines]
        lows = [k['low'] for k in klines]
        
        # 找到当前日期在K线中的下标
        try:
            idx = dates.index(trade_date_str)
        except ValueError:
            idx = -1
            for i in range(len(dates)-1, -1, -1):
                if dates[i] <= trade_date_str:
                    idx = i
                    break
            if idx < 120:
                continue
        
        current_price = closes[idx]
        
        # 尝试实时行情（盘中替换收盘价）
        rt = fetch_realtime_price(ts_code)
        if rt and rt['realtime']:
            current_price = rt['price']
        
        holding = holdings.get(ts_code)
        
        if holding:
            # ─── 持仓评估 ───
            buy_date = str(holding['buy_date']) if holding['buy_date'] else dates[0]
            cost = float(holding['cost_price'])
            
            # 计算持仓天数
            buy_idx = -1
            for i in range(len(dates)):
                if dates[i] == buy_date:
                    buy_idx = i
                    break
            if buy_idx < 0:
                for i in range(len(dates)-1, -1, -1):
                    if dates[i] <= buy_date:
                        buy_idx = i
                        break
            
            hold_days = idx - buy_idx if buy_idx >= 0 else 0
            
            # 计算最高价和回撤
            window = closes[buy_idx:idx+1]
            peak = max(window) if window else current_price
            buy_p = abs(cost) if cost != 0 else current_price  # 成本为负/0时用当前价（利润仓无成本基准）
            
            # ═══════════════════════════════════════════════
            # MAY双轨止血方案（2026-06-03）
            # 
            # A. 绝对止损（保护本金）：基于持仓成本
            #    当前价跌破成本价×(1-止损%) → 平仓
            #    只在成本>0时有效（负成本=利润仓，本金已安全）
            #
            # B. 移动止盈（保护浮盈）：基于期间最高价
            #    从持仓期间最高收盘价回撤≥X% → 止盈
            #    只在有浮盈时激活（peak > cost）
            # ═══════════════════════════════════════════════
            
            # A. 绝对止损（基于成本，保护本金）
            if cost > 0:
                # 从成本价计算的亏损比例
                loss_from_cost = (current_price - cost) / cost
                hit_abs_sl = 1 if loss_from_cost <= -sl_ratio else 0
            else:
                # 成本为0或负 → 本金已全部收回，绝对止损关闭
                loss_from_cost = None
                hit_abs_sl = 0
            
            # B. 移动止盈（基于最高价，保护浮盈）
            trailing_stop = float(strategy.get('trailing_stop_pct', 15) or 15)
            has_profit = peak > cost and cost > 0
            dd = (current_price - peak) / peak if peak > 0 else 0  # 从峰值回撤
            hit_ts = 1 if (has_profit and dd <= -trailing_stop/100) else 0
            
            # 综合盈亏（仅展示，不参与决策）
            if cost > 0:
                profit = (current_price - cost) / cost * 100
            else:
                profit = 999.9  # 利润仓，不计盈亏率
            
            # 确定当前检查点（v2.1 基于P6回测优化：5/15/25日检视）
            if hold_days <= 5:
                cp = 5
                days_to_cp = 5 - hold_days
                threshold = p1
                passed = None if hold_days < 5 else (current_score >= p1)
            elif hold_days <= 15:
                cp = 15
                days_to_cp = 15 - hold_days
                threshold = p2
                passed = current_score >= p2
            elif hold_days <= 25:
                cp = 25
                days_to_cp = 25 - hold_days
                threshold = p3
                passed = current_score >= p3
            else:
                cp = 30
                days_to_cp = 30 - hold_days
                threshold = p3
                if hold_days >= 30 or (hold_days % 5 == 0 and hold_days > 25):
                    passed = current_score >= p3
                else:
                    passed = 1  # 非检查日默认通过
            
            reduce_pct = float(strategy.get('reduce_pct', 0) or 0)
            reduce_flag = 1 if (reduce_pct > 0 and cost > 0 and (current_price - cost)/cost * 100 <= -reduce_pct) else 0
            
            # 最终行动
            if hit_abs_sl:
                action = 'STOP_LOSS'
                reason = f'⛔绝对止损：从成本{cost:.2f}亏损{loss_from_cost*100:.1f}%超过止损{sl_pct}%'
            elif hit_ts:
                action = 'SELL'
                reason = f'💰移动止盈：从高点{peak:.2f}回撤{-dd*100:.1f}%超过{trailing_stop}%，盈利保护'
            elif cp == 5 and hold_days >= 5 and not passed:
                action = 'SELL'
                reason = f'5日检查P6评分{current_score}<{p1}，不达标平仓'
            elif cp == 15 and hold_days >= 15 and not passed:
                action = 'SELL'
                reason = f'15日检查P6评分{current_score}<{p2}，不达标平仓'
            elif cp == 25 and hold_days >= 25 and not passed:
                action = 'SELL'
                reason = f'25日检查P6评分{current_score}<{p3}，不达标平仓'
            elif cp == 30 and hold_days >= 30 and not passed:
                action = 'SELL'
                reason = f'30日检查P6评分{current_score}<{p3}，不达标平仓'
            elif hold_days >= max_hold:
                action = 'SELL'
                reason = f'最大持有期{max_hold}日到期'
            else:
                if reduce_flag:
                    action = 'HOLD'
                    reason = f'亏损{profit:.1f}%超{reduce_pct:.0f}%减仓线，建议减半仓，P6评分{current_score}'
                else:
                    action = 'HOLD'
                    reason = f'已持有{hold_days}日，P6评分{current_score}，继续持有'
            
            results.append({
                'ts_code': ts_code, 'name': name,
                'trade_date': trade_date_str,
                'strategy_id': sid,
                'buy_score': round(current_score, 1),
                'holding_status': 'HOLDING',
                'hold_days': hold_days,
                'days_to_check': days_to_cp if cp <= 30 else 30 - hold_days,
                'current_checkpoint': cp,
                'buy_date': buy_date,
                'buy_price': round(buy_p, 3),
                'cost_price': round(cost, 3),
                'current_price_r': round(current_price, 3),
                'profit_pct': round(profit, 3),
                'checkpoint_score_check': round(current_score, 1),
                'checkpoint_threshold': threshold,
                'checkpoint_passed': passed if (hold_days == cp) else (1 if hold_days < cp else None),
                'peak_price': round(peak, 3),
                'drawdown_pct': round(dd*100, 3),
                'stop_loss_pct': sl_pct,
                'hit_stop_loss': hit_abs_sl,
                'reduce_flag': reduce_flag,
                'price_source': 'realtime' if (rt and rt.get('realtime')) else 'daily',
                'action': action,
                'action_reason': reason,
            })
            
        else:
            # ─── 未持仓——检查买入信号 ───
            # 冷却期：从strategy_signal历史查上次达到买入阈值的时间
            cur.execute("""
                SELECT MAX(trade_date) FROM strategy_signal
                WHERE ts_code=%s AND calibrated_score >= %s AND trade_date < %s
            """, (ts_code, buy_min, trade_date_str))
            r = cur.fetchone()
            last_buy_date = r.get('MAX(trade_date)') if r else None
            if last_buy_date:
                td = datetime.strptime(trade_date_str, '%Y-%m-%d')
                ld = datetime.strptime(str(last_buy_date), '%Y-%m-%d')
                days_since = (td - ld).days
            else:
                days_since = 999
            
            if last_buy_date is not None and days_since < cool_days:
                cur_action = 'WAIT'
                cur_reason = f'冷却期(距上次信号{days_since}日)，P6评分{current_score}'
            elif current_score >= buy_min:
                cur_action = 'BUY'
                cur_reason = f'评分{current_score}≥{buy_min}，触发买入条件'
            else:
                cur_action = 'WAIT'
                cur_reason = f'评分{current_score}<{buy_min}，等待买入'
            
            results.append({
                'ts_code': ts_code, 'name': name,
                'trade_date': trade_date_str,
                'strategy_id': sid,
                'buy_score': round(current_score, 1),
                'holding_status': 'NONE',
                'hold_days': 0,
                'days_to_check': None,
                'current_checkpoint': 0,
                'buy_date': None,
                'buy_price': None,
                'cost_price': None,
                'current_price_r': round(current_price, 3),
                'profit_pct': None,
                'checkpoint_score_check': None,
                'checkpoint_threshold': None,
                'checkpoint_passed': None,
                'peak_price': None,
                'drawdown_pct': None,
                'stop_loss_pct': sl_pct,
                'hit_stop_loss': 0,
                'reduce_flag': 0,
                'price_source': 'realtime' if (rt and rt.get('realtime')) else 'daily',
                'action': cur_action,
                'action_reason': cur_reason,
            })
    
    cur.close(); conn.close()
    return results


def save_results(conn, results):
    """批量写入评估结果到strategy_signal_daily"""
    cur = conn.cursor()
    
    sql = """INSERT INTO strategy_signal_daily 
    (ts_code, trade_date, strategy_id, buy_score, holding_status, hold_days, 
     days_to_check, current_checkpoint, buy_date, buy_price, cost_price, 
     current_price_r, profit_pct, checkpoint_score_check, checkpoint_threshold, 
     checkpoint_passed, peak_price, drawdown_pct, stop_loss_pct, 
     hit_stop_loss, reduce_flag, price_source, action, action_reason)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON DUPLICATE KEY UPDATE
      buy_score=VALUES(buy_score), holding_status=VALUES(holding_status),
      hold_days=VALUES(hold_days), days_to_check=VALUES(days_to_check),
      current_checkpoint=VALUES(current_checkpoint),
      buy_date=VALUES(buy_date), buy_price=VALUES(buy_price),
      cost_price=VALUES(cost_price), current_price_r=VALUES(current_price_r),
      profit_pct=VALUES(profit_pct),
      checkpoint_score_check=VALUES(checkpoint_score_check),
      checkpoint_threshold=VALUES(checkpoint_threshold),
      checkpoint_passed=VALUES(checkpoint_passed),
      peak_price=VALUES(peak_price), drawdown_pct=VALUES(drawdown_pct),
      hit_stop_loss=VALUES(hit_stop_loss),
      reduce_flag=VALUES(reduce_flag),
      price_source=VALUES(price_source),
      action=VALUES(action), action_reason=VALUES(action_reason)"""
    
    n = 0
    for r in results:
        try:
            cur.execute(sql, (
                r['ts_code'], r['trade_date'], r['strategy_id'], r['buy_score'],
                r['holding_status'], r['hold_days'], r['days_to_check'],
                r['current_checkpoint'],
                r['buy_date'] if r.get('buy_date') and r['buy_date'] != 'None' else None,
                None if r.get('buy_price') is None else float(r['buy_price']),
                None if r.get('cost_price') is None else float(r['cost_price']),
                r['current_price_r'], r['profit_pct'],
                r['checkpoint_score_check'], r['checkpoint_threshold'],
                r['checkpoint_passed'], r['peak_price'], r['drawdown_pct'],
                r['stop_loss_pct'], r['hit_stop_loss'], r.get('reduce_flag', 0),
                r.get('price_source', 'daily'), r['action'], r['action_reason']
            ))
            n += 1
        except Exception as e:
            print(f"  写入失败 {r['ts_code']}: {e}")
    
    conn.commit()
    return n


# ════════════════════════════════════════════
# API响应函数（供FastAPI调用）
# ════════════════════════════════════════════

def get_strategy_results(trade_date_str=None, strategy_id=1):
    """获取某日的策略评估结果"""
    if trade_date_str is None:
        trade_date_str = str(date.today())
    
    conn = get_conn()
    cur = conn.cursor(pymysql.cursors.DictCursor)
    
    # 获取策略
    cur.execute("SELECT * FROM strategy_config WHERE id=%s AND is_active=1", (strategy_id,))
    strategy = cur.fetchone()
    if not strategy:
        cur.close(); conn.close()
        return {'error': 'Strategy not found'}
    
    cur.execute("""
        SELECT ssd.*, wp.ts_code as in_watch
        FROM strategy_signal_daily ssd
        JOIN watch_pool wp ON ssd.ts_code = wp.ts_code AND wp.is_active=1
        WHERE ssd.trade_date=%s AND ssd.strategy_id=%s
        ORDER BY 
          CASE ssd.action 
            WHEN 'STOP_LOSS' THEN 0
            WHEN 'SELL' THEN 1
            WHEN 'BUY' THEN 2
            ELSE 3
          END,
          ssd.buy_score DESC
    """, (trade_date_str, strategy_id))
    
    signals = cur.fetchall()
    
    # 汇总统计
    action_counts = defaultdict(int)
    holding_count = 0
    total_profit = 0
    for s in signals:
        action_counts[s['action']] = action_counts.get(s['action'], 0) + 1
        if s['holding_status'] == 'HOLDING':
            holding_count += 1
            if s['profit_pct'] is not None:
                total_profit += float(s['profit_pct'])
    
    cur.close(); conn.close()
    
    return {
        'strategy': {
            'id': strategy['id'],
            'name': strategy['name'],
            'description': strategy['description'],
            'params': {
                'buy_min_score': strategy['buy_min_score'],
                'p1_score': strategy['p1_score'],
                'p2_score': strategy['p2_score'],
                'p3_score': strategy['p3_score'],
                'stop_loss_pct': float(strategy['stop_loss_pct']),
                'max_hold_days': strategy['max_hold_days'],
                'cool_days': strategy['cool_days'],
            }
        },
        'trade_date': trade_date_str,
        'total': len(signals),
        'holdings_count': holding_count,
        'action_summary': dict(action_counts),
        'total_profit_pct': round(total_profit, 2) if holding_count > 0 else 0,
        'signals': [{
            'ts_code': s['ts_code'],
            'name': s.get('stock_name', ''),
            'buy_score': float(s['buy_score']) if s['buy_score'] else 0,
            'holding_status': s['holding_status'],
            'hold_days': s['hold_days'],
            'current_checkpoint': s['current_checkpoint'],
            'days_to_check': s['days_to_check'],
            'cost_price': float(s['cost_price']) if s['cost_price'] else 0,
            'current_price': float(s['current_price_r']) if s['current_price_r'] else 0,
            'profit_pct': float(s['profit_pct']) if s['profit_pct'] else 0,
            'drawdown_pct': float(s['drawdown_pct']) if s['drawdown_pct'] else 0,
            'checkpoint_passed': bool(s['checkpoint_passed']) if s['checkpoint_passed'] is not None else None,
            'hit_stop_loss': bool(s['hit_stop_loss']),
            'action': s['action'],
            'action_reason': s['action_reason'],
        } for s in signals],
    }


# ════════════════════════════════════════════
# 主入口 run_daily
# ════════════════════════════════════════════

def run_daily(trade_date_str=None):
    """
    每日评估入口（16:00 cron调用）
    
    流程：
      1. 调用P6双轨引擎做全量评分 → 写入 strategy_signal
      2. 从 strategy_signal 读取P6评分
      3. 对上一步结果做阶梯策略评估 → 写入 strategy_signal_daily
    """
    if trade_date_str:
        td = datetime.strptime(trade_date_str, '%Y-%m-%d').date()
    else:
        td = date.today()
    
    print(f"\n{'='*60}")
    print(f"📊 阶梯策略每日评估 - {td}")
    print(f"{'='*60}")
    
    strategies = load_strategy_configs()
    print(f"活跃策略: {len(strategies)}个")
    
    # ─── 步骤1: 运行P6评分管道 ───
    try:
        run_p6_pipeline(td)
    except Exception as e:
        print(f"  ⚠️ P6管道执行异常: {e}，尝试直接用已有评分")
    
    # ─── 步骤2: 从strategy_signal读取P6评分，做阶梯策略评估 ───
    conn = get_conn()
    
    for s in strategies:
        print(f"\n▶ {s['name']} (ID={s['id']})")
        
        # 检查strategy_signal是否有当日P6评分
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM strategy_signal WHERE trade_date=%s", (str(td),))
        p6_count = cur.fetchone()[0]
        cur.close()
        
        if p6_count == 0:
            print(f"   ❌ strategy_signal 无{trade_date_str or td}日P6评分，跳过策略评估")
            continue
        
        print(f"   P6评分源: {p6_count}条 ✅")
        
        results = evaluate_strategy(td, s)
        print(f"   策略评估: {len(results)}只股票")
        
        n = save_results(conn, results)
        print(f"   已写入strategy_signal_daily: {n}条")
        
        # 统计
        actions = defaultdict(int)
        holdings = 0
        sell_signals = []
        buy_signals = []
        for r in results:
            actions[r['action']] += 1
            if r['holding_status'] == 'HOLDING':
                holdings += 1
                if r['action'] in ('SELL', 'STOP_LOSS'):
                    sell_signals.append(f"{r['name']}({r['action']}:{r['action_reason']})")
            if r['action'] == 'BUY':
                buy_signals.append(f"{r['name']}(评分{r['buy_score']})")
        
        print(f"   信号分布: {dict(actions)}")
        print(f"   持仓中: {holdings}只")
        if buy_signals:
            print(f"   买入信号: {', '.join(buy_signals[:10])}")
            if len(buy_signals) > 10: print(f"     ...还有{len(buy_signals)-10}只")
        if sell_signals:
            print(f"   卖出信号: {', '.join(sell_signals[:10])}")
            if len(sell_signals) > 10: print(f"     ...还有{len(sell_signals)-10}只")
    
    conn.close()
    print(f"\n✅ 完成")
    return True


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--api':
        import json
        td = sys.argv[2] if len(sys.argv) > 2 else str(date.today())
        result = get_strategy_results(td)
        print(json.dumps(result, ensure_ascii=False, default=str))
    else:
        td = sys.argv[1] if len(sys.argv) > 1 else None
        run_daily(td)
