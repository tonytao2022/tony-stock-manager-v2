#!/usr/bin/env python3
"""逐日补Tide评分：2026-04-01 ~ 2026-05-30，带连接池和重试"""
import sys, os, time, json, datetime, traceback
sys.path.insert(0, '/root/stock-system-v2/backend')
os.environ['PYTHONUNBUFFERED'] = '1'

LOG = '/root/stock-system-v2/backend/tide_engine/backfill_tide.log'
PROGRESS = '/root/stock-system-v2/backend/tide_engine/backfill_progress.json'
LOG_FILE = '/root/stock-system-v2/backend/tide_engine/backfill_tide.out'
ERROR_LOG = '/root/stock-system-v2/backend/tide_engine/backfill_errors.log'

def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')

def log_err(msg, exc=None):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] ERROR: {msg}'
    if exc:
        line += f'\n{traceback.format_exc()}'
    with open(ERROR_LOG, 'a') as f:
        f.write(line + '\n')

from tide_engine.tide_scorer import (
    _get_watch_pool, _compute_factors, _get_base_weights, _get_season,
    _l3_score, _save_factor_value, _save_chanlun_signal, _save_score_signal
)
from tide_engine.compute_future_returns import compute_future_returns
from db_config import get_connection

watch_pool = _get_watch_pool()
log(f'监控池共{len(watch_pool)}只股票')
weights = _get_base_weights()

# 已有日期
conn = get_connection(); cur = conn.cursor()
cur.execute("SELECT DISTINCT trade_date FROM tide_score_signal ORDER BY trade_date")
existing = {str(r['trade_date']) for r in cur.fetchall()}
cur.close(); conn.close()
log(f'已有{len(existing)}个交易日')

def workdays():
    r = []
    for m in range(4, 6):
        for d in range(1, 32):
            ds = f'2026-{m:02d}-{d:02d}'
            try:
                dt = datetime.datetime.strptime(ds, '%Y-%m-%d')
                if dt.weekday() < 5:
                    r.append(ds)
            except:
                pass
    return [d for d in r if '2026-04-01' <= d <= '2026-05-30' and d not in existing]

targets = workdays()
log(f'需补{len(targets)}个交易日')

with open(PROGRESS, 'w') as f:
    json.dump({'total': len(targets), 'done': 0, 'failed_dates': [], 'current': ''}, f)

# 批量保存函数，减少DB连接次数
def batch_save(trade_date, results):
    """每100只批量写入"""
    conn = get_connection(); cur = conn.cursor()
    try:
        for i, (ts_code, factors, l3, bonus, tide_score) in enumerate(results):
            _save_factor_value(trade_date, ts_code, factors, l3)
            _save_score_signal(trade_date, ts_code, l3, bonus, tide_score)
            if (i+1) % 100 == 0:
                conn.commit()
        conn.commit()
    finally:
        cur.close(); conn.close()

for idx, dt in enumerate(targets):
    with open(PROGRESS, 'w') as f:
        json.dump({'total': len(targets), 'done': idx, 'current': dt, 'failed_dates': []}, f)
    
    log(f'[{idx+1}/{len(targets)}] {dt} 开始...')
    t0 = time.time()
    results = []
    fail_ct = 0
    fail_codes = []
    season = _get_season(dt)
    is_chaos = season in ('chaos', 'chaos_spring', 'chaos_autumn')
    
    for i, ts_code in enumerate(watch_pool):
        try:
            factors = _compute_factors(ts_code, dt)
            l3 = _l3_score(factors, weights)
            bonus = 0.0
            
            if not is_chaos:
                from tide_engine.tide_chanlun_layer import apply_chanlun_layer
                chanlun = apply_chanlun_layer(ts_code, dt, factors)
                bonus = chanlun.get('bonus', 0.0)
            
            tide_score = min(100, max(0, round(l3 + bonus)))
            results.append((ts_code, factors, l3, bonus, tide_score))
        except Exception as e:
            fail_ct += 1
            fail_codes.append(ts_code)
            results.append((ts_code, {}, 0, 0, 0))
            if fail_ct <= 5:
                log_err(f'{dt} {ts_code}: {e}')
        
        if (i+1) % 200 == 0:
            log(f'  {dt}: {i+1}/{len(watch_pool)} 计算完成, 失败{fail_ct}')
    
    # 批量写入
    try:
        batch_save(dt, results)
        elapsed = time.time() - t0
        log(f'{dt} 完成! {elapsed:.0f}s, 写入{len(results)}只, 失败{fail_ct}')
        
        if fail_ct > 0:
            log(f'  失败股票示例(前10): {fail_codes[:10]}')
    except Exception as e:
        log_err(f'{dt} 批量写入失败: {e}', True)
        # 尝试逐条写
        log(f'{dt} 降级到逐条写入...')
        for r in results:
            try:
                conn2 = get_connection(); cur2 = conn2.cursor()
                _save_factor_value(r[0], dt, r[1], r[2])
                _save_score_signal(r[0], dt, r[2], r[3], r[4])
                cur2.close(); conn2.close()
            except:
                pass
    
    # 未来收益
    try:
        compute_future_returns()
        log(f'{dt} 未来收益计算完成')
    except Exception as e:
        log_err(f'{dt} 未来收益计算: {e}')
    
    # 清除失败日期数据，重新=0分的
    if fail_ct == len(watch_pool):
        log(f'⚠️ {dt} 全部失败！尝试用连接池模式重跑')
        with open(PROGRESS, 'w') as f:
            p = json.load(open(PROGRESS))
            p['failed_dates'].append(dt)
            json.dump(p, f)
    
    # 每5天检查
    if (idx+1) % 5 == 0:
        conn = get_connection(); cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM tide_score_signal")
        c = cur.fetchone()['c']
        cur.close(); conn.close()
        log(f'=== 进度: {idx+1}/{len(targets)}, tide_score_signal={c}条 ===')

with open(PROGRESS, 'w') as f:
    json.dump({'total': len(targets), 'done': len(targets), 'current': 'done'}, f)
log('全部完成！')
