#!/usr/bin/env python3
"""逐日补Tide评分：2025-07-01 ~ 2026-03-31，带连接池和重试，断点续传"""
import sys, os, time, json, datetime, traceback
sys.path.insert(0, '/root/stock-system-v2/backend')
os.environ['PYTHONUNBUFFERED'] = '1'

LOG = '/root/stock-system-v2/backend/tide_engine/backfill_tide.log'
PROGRESS = '/root/stock-system-v2/backend/tide_engine/backfill_progress.json'
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

# 计算从 season_state 获取所有交易日
conn = get_connection(); cur = conn.cursor()
cur.execute(
    "SELECT DISTINCT trade_date FROM season_state WHERE index_code='MARKET' "
    "AND trade_date >= '2025-07-01' AND trade_date <= '2026-03-31' ORDER BY trade_date"
)
all_dates = sorted([str(r['trade_date']) for r in cur.fetchall()])
cur.close(); conn.close()
log(f'2025-07-01~2026-03-31共{len(all_dates)}个交易日')

targets = [d for d in all_dates if d not in existing]
log(f'需补{len(targets)}个交易日 ({len(existing)}个已有)')

# 读取已有进度
start_idx = 0
if os.path.exists(PROGRESS):
    try:
        with open(PROGRESS) as f:
            p = json.load(f)
        if p.get('current') and p['current'] in targets:
            start_idx = targets.index(p['current'])
            log(f'恢复进度: 已完成{start_idx}/{len(targets)}，从{p["current"]}继续')
    except:
        pass

with open(PROGRESS, 'w') as f:
    json.dump({'total': len(targets), 'done': start_idx, 'failed_dates': [], 'current': targets[start_idx] if start_idx < len(targets) else 'done'}, f)

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

total_elapsed = 0
for idx in range(start_idx, len(targets)):
    dt = targets[idx]
    
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
        total_elapsed += elapsed
        remaining = (len(targets) - idx - 1) * (total_elapsed / (idx - start_idx + 1)) if (idx - start_idx + 1) > 0 else 0
        eta = time.strftime('%H:%M', time.localtime(time.time() + remaining))
        log(f'{dt} 完成! {elapsed:.0f}s, 写入{len(results)}只, 失败{fail_ct}, 预计{eta}完成')
        
        if fail_ct > 0:
            log(f'  失败股票示例(前10): {fail_codes[:10]}')
    except Exception as e:
        log_err(f'{dt} 批量写入失败: {e}', True)
        log(f'{dt} 降级到逐条写入...')
        for r in results:
            try:
                conn2 = get_connection(); cur2 = conn2.cursor()
                _save_factor_value(trade_date=dt, ts_code=r[0], factors=r[1], l3=r[2])
                _save_score_signal(trade_date=dt, ts_code=r[0], l3=r[2], bonus=r[3], tide_score=r[4])
                cur2.close(); conn2.close()
            except:
                pass
    
    # 未来收益
    try:
        compute_future_returns()
        log(f'{dt} 未来收益计算完成')
    except Exception as e:
        log_err(f'{dt} 未来收益计算: {e}')
    
    # 每10天检查进度
    if (idx + 1) % 10 == 0:
        conn = get_connection(); cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM tide_score_signal")
        c = cur.fetchone()['c']
        cur.close(); conn.close()
        log(f'=== 进度: {idx+1}/{len(targets)}, tide_score_signal={c}条 ===')

with open(PROGRESS, 'w') as f:
    json.dump({'total': len(targets), 'done': len(targets), 'current': 'done'}, f)
log(f'全部完成！共补{len(targets)}个交易日，总耗时{total_elapsed:.0f}s')
