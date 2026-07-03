#!/usr/bin/env python3
"""
daily_orch.py - 单管道调度器
每天 17:00 执行，原子步骤，带 PID 锁
"""
import os
import sys
import time
import json
import logging
from datetime import date, datetime

# 确保能找到后端模块
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))

from db_config import db_cursor

LOCK_FILE = '/tmp/stock_pipeline_v2.lock'
logger = logging.getLogger('daily_orch')
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s %(message)s')

STEPS = [
    {'name': 'kline',     'desc': '拉取K线数据'},
    {'name': 'moneyflow', 'desc': '资金流向'},
    {'name': 'season',    'desc': '季节判定'},
    {'name': 'chanlun',   'desc': '缠论分析'},
    {'name': 'score',     'desc': 'P6评分'},
    {'name': 'snapshot',  'desc': '生成快照'},
    {'name': 'holdings',  'desc': '更新持仓盈亏'},
    {'name': 'freshness', 'desc': '数据保鲜检查'},
]


def acquire_lock():
    if os.path.exists(LOCK_FILE):
        try:
            pid = open(LOCK_FILE).read().strip()
            if pid.isdigit() and os.path.isdir(f'/proc/{pid}'):
                logger.warning(f'管道已在运行 (PID={pid})，跳过')
                return False
        except:
            pass
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)


def run():
    """执行管道"""
    manual = '--manual' in sys.argv
    pipeline_name = 'manual_pipeline' if manual else 'daily_pipeline'
    trade_date = date.today()

    log_id = None
    start_time = time.time()

    # 写管道日志
    try:
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO pipeline_exec_log (pipeline_name, step_name, status, started_at)
                VALUES (%s, 'init', 'running', NOW())
            """, [pipeline_name])
            log_id = cur.lastrowid
    except:
        pass

    results = []

    for step in STEPS:
        step_start = time.time()
        step_status = 'success'
        error_msg = None

        try:
            logger.info(f'[Pipeline] 步骤 {step["name"]}: {step["desc"]}')

            if step['name'] == 'kline':
                _step_kline(trade_date)
            elif step['name'] == 'moneyflow':
                _step_moneyflow(trade_date)
            elif step['name'] == 'season':
                _step_season(trade_date)
            elif step['name'] == 'chanlun':
                _step_chanlun()
            elif step['name'] == 'score':
                _step_score(trade_date)
            elif step['name'] == 'snapshot':
                _step_snapshot(trade_date)
            elif step['name'] == 'holdings':
                _step_holdings()
            elif step['name'] == 'freshness':
                _step_freshness()

            logger.info(f'[Pipeline] ✅ {step["name"]} 完成')

        except Exception as e:
            step_status = 'failed'
            error_msg = str(e)
            logger.error(f'[Pipeline] ❌ {step["name"]} 失败: {e}')

        duration = int(time.time() - step_start)

        # 写日志
        if log_id:
            try:
                with db_cursor() as cur:
                    cur.execute("""
                        INSERT INTO pipeline_exec_log
                            (pipeline_name, step_name, status, started_at, finished_at,
                             duration_sec, error_msg)
                        VALUES (%s, %s, %s, DATE_SUB(NOW(), INTERVAL %s SECOND),
                                NOW(), %s, %s)
                    """, [pipeline_name, step['name'], step_status, duration,
                          duration, error_msg])
            except:
                pass

        results.append({
            'step': step['name'],
            'desc': step['desc'],
            'status': step_status,
            'duration_sec': duration,
        })

        if step_status == 'failed':
            break  # 一步失败暂停后续

    total_time = int(time.time() - start_time)

    logger.info(f'[Pipeline] 管道完成: {sum(1 for r in results if r["status"]=="success")}/{len(results)} 步骤成功, 耗时{total_time}秒')

    if manual:
        print(json.dumps({'results': results, 'total_time': total_time}, ensure_ascii=False, indent=2))

    release_lock()


# ─── 子步骤 ────────────────────────────────────────────────

def _step_kline(trade_date):
    """拉取K线（简单实现，从stock_basic遍历，调用Tushare）"""
    import requests as req

    token = os.environ.get('TUSHARE_TOKEN', '')
    if not token:
        # 从旧token临时借用
        token = 'd2b88da51a08626fd23b7be11418c593ccdee21a94d2e2aef4a334ad'
        os.environ['TUSHARE_TOKEN'] = token

    with db_cursor(commit=False) as cur:
        cur.execute("SELECT ts_code, name FROM watch_pool WHERE is_active=1")
        stocks = cur.fetchall()

    if not stocks:
        logger.warning('[Kline] 监控池为空')
        return

    url = 'http://api.tushare.pro'
    headers = {'Content-Type': 'application/json'}

    for s in stocks:
        code = s['ts_code']
        try:
            payload = {
                'api_name': 'daily',
                'token': token,
                'params': {'ts_code': code, 'start_date': trade_date.replace(year=trade_date.year - 1).strftime("%Y%m%d"),
                          'end_date': trade_date.strftime("%Y%m%d")}
            }
            resp = req.post(url, json=payload, headers=headers, timeout=15)
            data = resp.json()

            if data.get('code') != 0:
                logger.warning(f'[Kline] {code} Tushare返回错误: {data.get("msg")}')
                continue

            items = data.get('data', {}).get('items', [])
            fields = data.get('data', {}).get('fields', [])

            if not items:
                continue

            # 找出各字段索引
            idx_ts = fields.index('ts_code') if 'ts_code' in fields else -1
            idx_td = fields.index('trade_date') if 'trade_date' in fields else -1
            idx_o = fields.index('open') if 'open' in fields else -1
            idx_h = fields.index('high') if 'high' in fields else -1
            idx_l = fields.index('low') if 'low' in fields else -1
            idx_c = fields.index('close') if 'close' in fields else -1
            idx_pc = fields.index('pre_close') if 'pre_close' in fields else -1
            idx_ch = fields.index('pct_chg') if 'pct_chg' in fields else -1
            idx_v = fields.index('vol') if 'vol' in fields else -1
            idx_a = fields.index('amount') if 'amount' in fields else -1

            with db_cursor() as cur:
                for item in items:
                    try:
                        cur.execute("""
                            INSERT INTO daily_kline
                                (ts_code, trade_date, open, high, low, close,
                                 pre_close, change_pct, vol, amount)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON DUPLICATE KEY UPDATE
                                open=VALUES(open), high=VALUES(high), low=VALUES(low),
                                close=VALUES(close), pre_close=VALUES(pre_close),
                                change_pct=VALUES(change_pct), vol=VALUES(vol),
                                amount=VALUES(amount)
                        """, (
                            item[idx_ts] if idx_ts >= 0 else code,
                            item[idx_td] if idx_td >= 0 else '',
                            float(item[idx_o])  if idx_o >= 0 else 0,
                            float(item[idx_h])  if idx_h >= 0 else 0,
                            float(item[idx_l])  if idx_l >= 0 else 0,
                            float(item[idx_c])  if idx_c >= 0 else 0,
                            float(item[idx_pc])  if idx_pc >= 0 else 0,
                            float(item[idx_ch])  if idx_ch >= 0 else 0,
                            float(item[idx_v])  if idx_v >= 0 else 0,
                            float(item[idx_a])  if idx_a >= 0 else 0,
                        ))
                    except Exception as e:
                        logger.debug(f'[Kline] {code} 写入行失败: {e}')
                        continue

        except Exception as e:
            logger.error(f'[Kline] {code} 拉取失败: {e}')
            time.sleep(0.5)  # 限流


def _step_moneyflow(trade_date):
    """拉取全市场资金流向数据到moneyflow和money_flow两张表"""
    import requests as req
    token = os.environ.get('TUSHARE_TOKEN', '')
    if not token:
        logger.warning('[MoneyFlow] TUSHARE_TOKEN 未设置，跳过')
        return
    
    td = trade_date.strftime('%Y%m%d')
    url = 'http://api.tushare.pro'
    headers = {'Content-Type': 'application/json'}
    
    payload = {
        'api_name': 'moneyflow',
        'token': token,
        'params': {'trade_date': td}
    }
    
    try:
        resp = req.post(url, json=payload, headers=headers, timeout=30)
        data = resp.json()
        if data.get('code') != 0:
            logger.warning(f'[MoneyFlow] Tushare返回错误: {data.get("msg")}')
            return
        items = data.get('data', {}).get('items', [])
        fields = data.get('data', {}).get('fields', [])
        if not items:
            logger.info(f'[MoneyFlow] {td} 无数据（可能非交易日）')
            return
        
        # 字段索引
        def fi(name):
            try: return fields.index(name)
            except: return -1
        idx_ts = fi('ts_code')
        idx_td = fi('trade_date')
        idx_nm = fi('net_mf_amount')
        idx_bl = fi('buy_lg_amount')
        idx_sl = fi('sell_lg_amount')
        idx_bel = fi('buy_elg_amount')
        idx_sel = fi('sell_elg_amount')
        idx_bs = fi('buy_sm_amount')
        idx_ss = fi('sell_sm_amount')
        
        saved_mf = 0
        saved_flow = 0
        with db_cursor() as cur:
            for item in items:
                tc = item[idx_ts] if idx_ts >= 0 else ''
                tdate = trade_date.strftime('%Y-%m-%d')
                try:
                    nm = float(item[idx_nm]) if idx_nm >= 0 else 0
                    bl = float(item[idx_bl]) if idx_bl >= 0 else 0
                    sl = float(item[idx_sl]) if idx_sl >= 0 else 0
                    bel = float(item[idx_bel]) if idx_bel >= 0 else 0
                    sel = float(item[idx_sel]) if idx_sel >= 0 else 0
                    bs = float(item[idx_bs]) if idx_bs >= 0 else 0
                    ss = float(item[idx_ss]) if idx_ss >= 0 else 0
                except:
                    continue
                
                # 写入moneyflow表（保留全字段给评分引擎）
                try:
                    cur.execute("""
                        INSERT INTO moneyflow (ts_code, trade_date, net_mf_amount, buy_lg_amount, sell_lg_amount, buy_sm_amount, sell_sm_amount, buy_elg_amount, sell_elg_amount)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON DUPLICATE KEY UPDATE net_mf_amount=VALUES(net_mf_amount),
                            buy_lg_amount=VALUES(buy_lg_amount), sell_lg_amount=VALUES(sell_lg_amount),
                            buy_sm_amount=VALUES(buy_sm_amount), sell_sm_amount=VALUES(sell_sm_amount),
                            buy_elg_amount=VALUES(buy_elg_amount), sell_elg_amount=VALUES(sell_elg_amount)
                    """, (tc, tdate, nm, bl, sl, bs, ss, bel, sel))
                    saved_mf += 1
                except: pass
                
                # 写入money_flow表（精简字段给其他模块）
                try:
                    main_net = bl - sl + bel - sel  # 主力净=大单+特大单
                    retail_net = bs - ss             # 散户净=小单
                    total_buy = bl + bel
                    total_sell = sl + sel
                    net_val = nm
                    cur.execute("""
                        INSERT INTO money_flow (ts_code, trade_date, main_net, retail_net, buy_value, sell_value, net_value)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                        ON DUPLICATE KEY UPDATE main_net=VALUES(main_net), retail_net=VALUES(retail_net),
                            buy_value=VALUES(buy_value), sell_value=VALUES(sell_value), net_value=VALUES(net_value)
                    """, (tc, tdate, main_net, retail_net, total_buy, total_sell, net_val))
                    saved_flow += 1
                except: pass
        
        logger.info(f'[MoneyFlow] moneyflow入库{saved_mf}条, money_flow入库{saved_flow}条')
    except Exception as e:
        logger.error(f'[MoneyFlow] 拉取失败: {e}')


def _step_season(trade_date):
    """季节判定"""
    from engines.season import detect_season
    detect_season(trade_date)


def _step_chanlun():
    """缠论分析"""
    from engines.chanlun import analyze_all
    analyze_all()


def _step_score(trade_date):
    """P6评分（双轨引擎）"""
    from engines.p6_scorer import run_scoring
    run_scoring(trade_date)


def _step_snapshot(trade_date):
    """生成监控池快照"""
    from db_config import db_cursor as _dc

    with _dc() as cur:
        cur.execute("""
            INSERT INTO watch_pool_snapshot
                (ts_code, trade_date, v_score, raw_score, trend_score,
                 momentum_score, signal_type, signal_label, season, regime,
                 name, industry, close_price, change_pct, position_pct,
                 ret_5d, ret_10d, ret_20d)
            SELECT
                ss.ts_code, ss.trade_date,
                ss.composite_score, ss.raw_score, ss.trend_score,
                ss.momentum_score,
                ss.signal_type, ss.signal_label, ss.season, ss.regime,
                COALESCE(sb.name, ''),
                COALESCE(sb.industry, ''),
                0, 0, ss.position_pct,
                0, 0, 0
            FROM strategy_signal ss
            LEFT JOIN stock_basic sb ON ss.ts_code = sb.ts_code
            WHERE ss.trade_date=%s
              AND ss.ts_code IN (SELECT ts_code FROM watch_pool WHERE is_active=1)
            ON DUPLICATE KEY UPDATE
                v_score=VALUES(v_score), raw_score=VALUES(raw_score),
                trend_score=VALUES(trend_score),
                momentum_score=VALUES(momentum_score),
                signal_type=VALUES(signal_type),
                signal_label=VALUES(signal_label),
                season=VALUES(season), regime=VALUES(regime),
                name=VALUES(name), industry=VALUES(industry),
                position_pct=VALUES(position_pct)
        """, [trade_date])

    logger.info('[Snapshot] 快照生成完成')


def _step_holdings():
    """更新持仓盈亏 + 持仓股自动加入监控池"""
    from engines.strategy import check_all_holdings
    check_all_holdings()
    
    # 持仓股自动加入监控池（MAY评审意见固化）
    with db_cursor() as cur:
        # 查出持仓中不在监控池的股票
        cur.execute("""
            INSERT IGNORE INTO watch_pool (ts_code, name, industry, reason, is_active)
            SELECT ph.ts_code, ph.name,
                   COALESCE(sb.industry, ''),
                   '持仓股票自动入池', 1
            FROM portfolio_holdings ph
            LEFT JOIN stock_basic sb ON ph.ts_code = sb.ts_code
            WHERE ph.status IN ('hold', 'locked')
              AND ph.ts_code NOT IN (SELECT ts_code FROM watch_pool WHERE is_active=1)
        """)
        affected = cur.rowcount
        if affected > 0:
            logger.info(f'[Holdings] 自动将{affected}只持仓股加入监控池')


def _step_freshness():
    """数据保鲜检查"""
    with db_cursor(commit=False) as cur:
        for table in ['daily_kline', 'strategy_signal', 'season_state']:
            cur.execute(f"SELECT MAX(trade_date) as d FROM {table}")
            row = cur.fetchone()
            latest = str(row['d']) if row and row['d'] else '无数据'
            logger.info(f'[Freshness] {table}: 最新日期={latest}')


if __name__ == '__main__':
    if not acquire_lock():
        sys.exit(0)
    try:
        run()
    finally:
        release_lock()
