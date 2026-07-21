#!/usr/bin/env python3
"""
盘中实时信号Watchdog — V2 全池监控版
========================================
每30~60分钟轮询全监控池(620只)的腾讯实时行情
计算买卖盘强度比，输出盘中信号修正建议

V2升级：不再按持仓+高分池，直接扫全池
批量拉取(每批60只)，620只仅需1~2秒
"""
import sys, os, json, time, requests
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, '/root/stock-system-v2/backend')
from db_config import get_connection

GT_URL = 'http://qt.gtimg.cn/q='


def parse_batch_response(text):
    """批量解析腾讯行情返回文本（多只股票）"""
    results = {}
    for line in text.strip().split('\n'):
        line = line.strip()
        if not line or '="' not in line:
            continue
        code = line.split('="')[0].strip('v_')
        data_str = line.split('="')[1].rstrip('";')
        parts = data_str.split('~')
        if len(parts) < 40:
            continue
        try:
            results[code] = {
                'name': parts[1],
                'code': parts[2],
                'price': float(parts[3]) if parts[3] else 0,
                'change_pct': float(parts[32]) if parts[32] else 0,
                'buy_volumes': [],
                'sell_volumes': [],
            }
            for i in range(5):
                idx = 10 + i * 2
                results[code]['buy_volumes'].append(
                    int(float(parts[idx])) if len(parts) > idx and parts[idx] else 0)
            for i in range(5):
                idx = 20 + i * 2
                results[code]['sell_volumes'].append(
                    int(float(parts[idx])) if len(parts) > idx and parts[idx] else 0)
        except:
            continue
    return results


def batch_fetch_all(codes):
    """批量拉取所有股票腾讯行情（每批60只）"""
    tencent_codes = []
    code_map = {}  # tencent格式 -> 原始ts_code
    
    for c in codes:
        short = c.split('.')[0]
        if c.endswith('.SH'):
            gt = 'sh' + short
        else:
            gt = 'sz' + short
        tencent_codes.append(gt)
        code_map[gt] = c
    
    batch_size = 60
    all_results = {}
    
    for i in range(0, len(tencent_codes), batch_size):
        batch = tencent_codes[i:i+batch_size]
        url = GT_URL + ','.join(batch)
        try:
            r = requests.get(url, timeout=10,
                             headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code == 200:
                parsed = parse_batch_response(r.text)
                for gt_code, data in parsed.items():
                    if gt_code in code_map:
                        all_results[code_map[gt_code]] = data
        except Exception as e:
            print(f"  ⚠️ 批次{i//batch_size+1}错误: {str(e)[:60]}", flush=True)
    
    return all_results


def calc_buy_sell_ratio(quote):
    """计算买卖盘强度比"""
    buy_sum = sum(quote['buy_volumes'])
    sell_sum = sum(quote['sell_volumes'])
    if sell_sum == 0:
        return 99.9
    return round(buy_sum / sell_sum, 2)


def assess_signal(ratio, change_pct):
    """根据买卖盘比和涨跌幅评估信号"""
    if ratio >= 1.5 and change_pct > 0:
        return '🟢强势买入', f'买盘旺盛({ratio:.1f}倍)+上涨{change_pct:.2f}%'
    elif ratio >= 1.2 and change_pct > 0:
        return '🟡买盘偏强', f'买盘({ratio:.1f}倍)+上涨{change_pct:.2f}%'
    elif ratio >= 0.8 and ratio < 1.2:
        return '⚪中性', f'买卖均衡({ratio:.1f}倍)'
    elif ratio < 0.8 and ratio > 0 and change_pct < 0:
        return '🔴资金出逃', f'卖盘强势({1/ratio:.2f}倍)+下跌{change_pct:.2f}%'
    elif ratio < 0.6 and ratio > 0:
        return '🔴卖盘强势', f'卖盘({1/ratio:.2f}倍)'
    elif ratio == 0:
        return '🔴卖盘独大', '卖盘占据、无买盘'
    else:
        return '⚪中性偏弱', f'买卖比{ratio:.1f}倍'


def get_watch_codes():
    """V2: 全监控池620只"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT ts_code FROM watch_pool WHERE is_active=1")
    codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return codes


def save_snapshot(signals):
    """保存盘中信号到数据库"""
    conn = get_connection()
    cur = conn.cursor()
    
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    today = datetime.now().strftime('%Y-%m-%d')
    
    # V2: 高频写入，用批量插入
    for s in signals:
        cur.execute("""
            INSERT INTO intraday_signals (ts_code, trade_date, check_time,
                price, change_pct, buy_sell_ratio, signal_label, signal_detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                check_time=VALUES(check_time), price=VALUES(price),
                change_pct=VALUES(change_pct), buy_sell_ratio=VALUES(buy_sell_ratio),
                signal_label=VALUES(signal_label), signal_detail=VALUES(signal_detail)
        """, (s['ts_code'], today, now, s['price'], s['change_pct'],
              s['ratio'], s['label'], s['detail']))
    
    conn.commit()
    cur.close()
    conn.close()


def run_check():
    """执行一次盘中检查 — 分两批交错，每次只跑当前分钟奇偶对应的一半
    
    方案A：全池620只分2批（奇偶位均匀分配），每次check只跑一批
    cron每15分钟跑一次 check，自动根据分钟奇偶切换批次
    等效全池每30分钟完整一轮（A+B两批覆盖），但高频信号每15分钟到一次
    """
    start = time.time()
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    minute = now.minute
    
    # 清理过期信号（每天第一次运行）
    if minute < 20:  # 第一个15分钟窗口清理一次即可
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM intraday_signals WHERE trade_date < %s", (today,))
            if cur.rowcount > 0:
                print(f"  🧹 清空{cur.rowcount}条过期信号", flush=True)
            cur.close()
            conn.close()
        except:
            pass
    
    # 根据分钟奇偶决定本批次：偶数分钟→批次A(索引0)，奇数分钟→批次B(索引1)
    batch_idx = minute % 2  # 0或1
    batch_label = 'A' if batch_idx == 0 else 'B'
    
    all_codes, batch_codes = get_watch_codes_batch(batch_idx, total_batches=2)
    if not batch_codes:
        print(f"[{now.strftime('%H:%M')}] 批次{batch_label}无股票数据", flush=True)
        return
    
    print(f"[{now.strftime('%H:%M')}] 批次{batch_label}: 拉取{len(batch_codes)}只(全池{len(all_codes)}只分2批交错)...", flush=True)
    all_quotes = batch_fetch_all(batch_codes)
    fetch_time = time.time() - start
    print(f"  ✅ 批次{batch_label}: 拉取{len(all_quotes)}只, 耗时{fetch_time:.1f}秒", flush=True)
    
    if not all_quotes:
        print(f"  ⚠️ 批次{batch_label}未获取到有效数据", flush=True)
        return
    
    # 计算信号
    signals = []
    stats = {'🟢': 0, '🟡': 0, '⚪': 0, '🔴': 0}
    
    for code, quote in all_quotes.items():
        ratio = calc_buy_sell_ratio(quote)
        label_text, detail = assess_signal(ratio, quote['change_pct'])
        signals.append({
            'ts_code': code,
            'name': quote['name'],
            'price': quote['price'],
            'change_pct': round(quote['change_pct'], 2),
            'ratio': ratio,
            'label': label_text,
            'detail': detail,
        })
        for key in stats:
            if key in label_text:
                stats[key] += 1
                break
    
    try:
        save_snapshot(signals)
    except Exception as e:
        print(f"  ❌ 批次{batch_label}保存失败: {e}", flush=True)
    
    elapsed = time.time() - start
    print(f"  💾 批次{batch_label}: {len(signals)}条保存 | 🟢{stats['🟢']} 🟡{stats['🟡']} ⚪{stats['⚪']} 🔴{stats['🔴']} | 耗时{elapsed:.1f}秒", flush=True)


def get_watch_codes_batch(batch_idx, total_batches):
    """获取监控池中按批次拆分的股票代码"""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT ts_code FROM watch_pool WHERE is_active=1 ORDER BY ts_code")
    all_codes = [r['ts_code'] for r in cur.fetchall()]
    cur.close()
    conn.close()
    
    # 按总批数拆分 (均匀分配，不是简单切块)
    batch_codes = [all_codes[i] for i in range(batch_idx, len(all_codes), total_batches)]
    return all_codes, batch_codes


def run_check_batch(label, codes, batch_total=1, batch_idx=0):
    """执行一批股票的盘中检查"""
    start = time.time()
    
    print(f"[{datetime.now().strftime('%H:%M')}] 批次{label}: 拉取{len(codes)}只腾讯行情...", flush=True)
    all_quotes = batch_fetch_all(codes)
    fetch_time = time.time() - start
    print(f"  ✅ 批次{label}: 拉取{len(all_quotes)}只, 耗时{fetch_time:.1f}秒", flush=True)
    
    if not all_quotes:
        print(f"  ⚠️ 批次{label}未获取到有效数据", flush=True)
        return
    
    signals = []
    stats = {'🟢': 0, '🟡': 0, '⚪': 0, '🔴': 0}
    
    for code, quote in all_quotes.items():
        ratio = calc_buy_sell_ratio(quote)
        label_text, detail = assess_signal(ratio, quote['change_pct'])
        signals.append({
            'ts_code': code,
            'name': quote['name'],
            'price': quote['price'],
            'change_pct': round(quote['change_pct'], 2),
            'ratio': ratio,
            'label': label_text,
            'detail': detail,
        })
        for key in stats:
            if key in label_text:
                stats[key] += 1
                break
    
    try:
        save_snapshot(signals)
    except Exception as e:
        print(f"  ❌ 批次{label}保存失败: {e}", flush=True)
    
    elapsed = time.time() - start
    print(f"  💾 批次{label}: {len(signals)}条保存 | 🟢{stats['🟢']} 🟡{stats['🟡']} ⚪{stats['⚪']} 🔴{stats['🔴']} | 耗时{elapsed:.1f}秒", flush=True)


def run_polling(interval_min=30):
    """持续轮询模式"""
    # === 清理过期信号（启动时跑一次）===
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM intraday_signals WHERE trade_date < %s", (today,))
        if cur.rowcount > 0:
            print(f"  🧹 清空{cur.rowcount}条过期信号", flush=True)
        cur.close()
        conn.close()
    except:
        pass
    
    print(f"🚀 Watchdog V2 启动 | 全池620只分2批轮询, 等效15分钟全扫一轮", flush=True)
    
    # 分2批轮询：每批间隔7.5分钟
    # 奇数分钟窗口跑[0]，偶数分钟窗口跑[1]
    TOTAL_BATCHES = 2
    BATCH_GAP_SECONDS = 450  # 7.5分钟
    
    # 预拉一次全池代码列表，后续每轮重新拉取（监控池可能变化）
    all_codes, batch_0_codes = get_watch_codes_batch(0, TOTAL_BATCHES)
    _, batch_1_codes = get_watch_codes_batch(1, TOTAL_BATCHES)
    print(f"  📊 批次A: {len(batch_0_codes)}只 | 批次B: {len(batch_1_codes)}只 | 合计{len(all_codes)}只", flush=True)
    
    batch_idx = 0
    while True:
        try:
            if batch_idx == 0:
                run_check_batch('A', batch_0_codes)
            else:
                run_check_batch('B', batch_1_codes)
        except Exception as e:
            print(f"  ❌ 批次{'A' if batch_idx==0 else 'B'}异常: {e}", flush=True)
            import traceback
            traceback.print_exc()
        
        # 切换批次
        batch_idx = 1 - batch_idx
        
        next_time = datetime.now() + timedelta(seconds=BATCH_GAP_SECONDS)
        print(f"⏰ 下批({['A','B'][batch_idx]})检查: {next_time.strftime('%H:%M:%S')}", flush=True)
        time.sleep(BATCH_GAP_SECONDS)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('action', nargs='?', default='check',
                        choices=['check', 'poll', 'init'])
    parser.add_argument('--interval', type=int, default=30, help='轮询间隔(分钟)')
    args = parser.parse_args()
    
    if args.action == 'init':
        from db_config import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS intraday_signals (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ts_code VARCHAR(20) NOT NULL,
                trade_date DATE NOT NULL,
                check_time DATETIME NOT NULL,
                price DECIMAL(12,2) DEFAULT 0,
                change_pct DECIMAL(6,2) DEFAULT 0,
                buy_sell_ratio DECIMAL(6,2) DEFAULT 0,
                signal_label VARCHAR(20) DEFAULT '',
                signal_detail VARCHAR(200) DEFAULT '',
                UNIQUE KEY uk_code_date (ts_code, trade_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        conn.commit()
        conn.close()
        print("✅ intraday_signals 表已创建")
    elif args.action == 'poll':
        run_polling(interval_min=args.interval)
    else:
        run_check()
